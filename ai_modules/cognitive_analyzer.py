"""Agente de Análisis Cognitivo (ai_modules/cognitive_analyzer.py) — Tarea S1-1 / R2.

Transforma `List[InteraccionValidada]` en el Contrato 2 (`AnalisisCognitivo`).
Es una función transformadora sin estado: no importa LangGraph ni
`storage/oci_client.py`. La persistencia y el enrutamiento son
responsabilidad de `orchestration/pipeline_runner.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import Counter

import structlog
from pydantic import ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ai_modules.multiprovider_agent import CognitiveAgentFactory
from ai_modules.prompts import SYSTEM_PROMPT
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    PuntuacionRelevancia,
    ResumenComunidad,
)

logger = structlog.get_logger("ai_modules.cognitive_analyzer")

PROVEEDOR_POR_DEFECTO = "openai"
MAX_REINTENTOS_DEFECTO = 3
CONCURRENCIA_DEFECTO = 5
REINTENTOS_VALIDACION = 2
BACKOFF_BASE_SEG = 1.0
BACKOFF_MAX_SEG = 10.0
MAX_CHARS_TEXTO = 2000
FRACCION_INICIAL_TRUNCADO = 0.2

# Marcador estable: el resumen identifica registros degradados por este
# prefijo en vez de depender de texto libre incrustado en la lógica.
PREFIJO_DEGRADACION = "Degradación por error de servicio tras reintentos"

SENTIMIENTOS_OFICIALES = (
    "Altamente Positivo",
    "Positivo",
    "Neutro",
    "Dificultad/Negativo",
)

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_CREDENCIAL = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,}"
    r"|bearer\s+[A-Za-z0-9._-]{8,}"
    r"|(?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*\S+)"
)
_RE_TELEFONO = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")


def _enmascarar_pii(texto: str) -> str:
    """Reduce (no garantiza eliminar) PII antes de enviar texto al proveedor LLM."""
    texto = _RE_EMAIL.sub("[EMAIL_PROTEGIDO]", texto)
    texto = _RE_CREDENCIAL.sub("[CREDENCIAL_PROTEGIDA]", texto)
    return _RE_TELEFONO.sub("[TELEFONO_PROTEGIDO]", texto)


def _truncar_texto_semantico(texto: str, max_chars: int = MAX_CHARS_TEXTO) -> str:
    """Conserva 20% inicial (contexto) y 80% final (desenlace/error) del presupuesto."""
    if len(texto) <= max_chars:
        return texto
    inicio = int(max_chars * FRACCION_INICIAL_TRUNCADO)
    fin = max_chars - inicio
    return f"{texto[:inicio]}\n[... contenido intermedio truncado ...]\n{texto[-fin:]}"


def _leer_entero_env(nombre: str, defecto: int) -> int:
    """Único punto de lectura de entorno del módulo; valores inválidos usan el defecto."""
    try:
        valor = int(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto
    return valor if valor >= 1 else defecto


def _resolver_proveedor(provider: str | None) -> str:
    return (provider or os.environ.get("LLM_PROVIDER") or PROVEEDOR_POR_DEFECTO).lower()


def _crear_mensaje_degradado(source_id: str, error: Exception) -> MensajeAnalizado:
    """Fallback semántico: puntaje 0, no publicable, `source_id` intacto.

    Solo se registra el tipo de excepción: el mensaje original podría contener
    credenciales o fragmentos del prompt (M09).
    """
    return MensajeAnalizado(
        source_id=source_id,
        sentimiento="Neutro",
        categoria_enrutamiento="Duda",
        regla_aplicada="Degradación elegante por indisponibilidad de inferencia",
        temas=["Sin_Clasificar"],
        entidades_relevantes=[],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=0, utilidad_comunitaria=0, claridad_contexto=0
        ),
        motivo_seleccion=f"{PREFIJO_DEGRADACION}: {type(error).__name__}",
        requiere_soporte=False,
        apto_para_publicacion=False,
    )


def _preparar_entrada(interaccion: InteraccionValidada) -> str:
    texto = _truncar_texto_semantico(_enmascarar_pii(interaccion.texto))
    datos = interaccion.model_copy(update={"texto": texto}).model_dump(mode="json")
    return json.dumps(datos, ensure_ascii=False)


def _espera_backoff(intento: int) -> float:
    base = min(BACKOFF_MAX_SEG, BACKOFF_BASE_SEG * 2**intento)
    return max(0.0, base + random.uniform(-0.5, 0.5))


async def _ejecutar_inferencia_con_resiliencia(
    agent,
    interaccion: InteraccionValidada,
    proveedor: str,
    max_reintentos: int,
) -> tuple[MensajeAnalizado, int]:
    """Devuelve (mensaje, tokens_cacheados). Nunca propaga errores del proveedor."""
    entrada = _preparar_entrada(interaccion)
    fallos_validacion = 0
    ultimo_error: Exception = RuntimeError("sin_intentos")
    for intento in range(max_reintentos + 1):
        # Tras un fallo de esquema se fuerza temperatura 0 para reducir variabilidad.
        settings = {"temperature": 0.0} if fallos_validacion else None
        try:
            resultado = await agent.run(entrada, model_settings=settings)
            mensaje: MensajeAnalizado = resultado.output
            # Invariante de trazabilidad: el LLM no decide el source_id.
            mensaje = mensaje.model_copy(update={"source_id": interaccion.id})
            tokens = CognitiveAgentFactory.extract_cached_tokens(proveedor, resultado.usage())
            return mensaje, tokens
        except (ValidationError, UnexpectedModelBehavior) as exc:
            ultimo_error = exc
            fallos_validacion += 1
            if fallos_validacion > REINTENTOS_VALIDACION:
                break
        except Exception as exc:  # noqa: BLE001 — frontera de red: degradar, no tumbar el lote
            ultimo_error = exc
            if intento < max_reintentos:
                await asyncio.sleep(_espera_backoff(intento))
    logger.warning(
        "inferencia_degradada",
        source_id=interaccion.id,
        error_tipo=type(ultimo_error).__name__,
    )
    return _crear_mensaje_degradado(interaccion.id, ultimo_error), 0


def _es_degradado(m: MensajeAnalizado) -> bool:
    return m.puntuacion_relevancia.total == 0 and m.motivo_seleccion.startswith(
        PREFIJO_DEGRADACION
    )


def _calcular_resumen_comunidad(
    mensajes: list[MensajeAnalizado], total_recibidos_en_lote: int
) -> ResumenComunidad:
    """Agregación determinista; el LLM nunca calcula estadísticas del lote."""
    degradados = sum(_es_degradado(m) for m in mensajes)
    conteo: dict[str, int] = dict.fromkeys(SENTIMIENTOS_OFICIALES, 0)
    for m in mensajes:
        conteo[m.sentimiento] += 1
    moda = max(conteo, key=lambda k: conteo[k]) if mensajes else "Neutro"
    temas = [t for m in mensajes for t in m.temas]
    temas_top = [t for t, _ in Counter(temas).most_common(5)] or ["General"]
    alertas = [f"[{m.source_id}] {m.motivo_seleccion}" for m in mensajes if m.requiere_soporte]
    validos = len(mensajes) - degradados
    if validos < 1:
        # El contrato exige registros_validos >= 1; se deja constancia de la caída total.
        alertas.append("Todos los registros del lote fueron degradados por indisponibilidad del LLM")
        validos = 1
    return ResumenComunidad(
        total_interacciones_procesadas=max(1, total_recibidos_en_lote),
        registros_validos=validos,
        registros_rechazados=degradados,
        sentimiento_predominante=moda,
        distribucion_sentimiento=conteo,
        temas_principales=temas_top,
        alertas_soporte=alertas,
    )


async def analizar_lote_cognitivo(
    interacciones: list[InteraccionValidada],
    run_id: str,
    request_id: str,
    provider: str | None = None,
    max_concurrency: int | None = None,
) -> AnalisisCognitivo:
    """Analiza un lote de interacciones ya validadas y devuelve el Contrato 2.

    `max_concurrency` y los reintentos se resuelven desde
    COGNITIVE_ANALYZER_CONCURRENCY y LLM_MAX_RETRIES si no se pasan.
    """
    if not interacciones:
        raise ValueError("analizar_lote_cognitivo requiere al menos una interacción validada")

    proveedor = _resolver_proveedor(provider)
    concurrencia = max_concurrency or _leer_entero_env(
        "COGNITIVE_ANALYZER_CONCURRENCY", CONCURRENCIA_DEFECTO
    )
    max_reintentos = _leer_entero_env("LLM_MAX_RETRIES", MAX_REINTENTOS_DEFECTO)
    agent = CognitiveAgentFactory.create_agent(proveedor, SYSTEM_PROMPT, MensajeAnalizado)
    semaforo = asyncio.Semaphore(concurrencia)

    async def _procesar(i: InteraccionValidada) -> tuple[MensajeAnalizado, int]:
        async with semaforo:
            return await _ejecutar_inferencia_con_resiliencia(agent, i, proveedor, max_reintentos)

    inicio = time.perf_counter()
    resultados = await asyncio.gather(*(_procesar(i) for i in interacciones))
    total_seg = time.perf_counter() - inicio

    mensajes = [m for m, _ in resultados]
    tokens_cacheados = sum(t for _, t in resultados)
    resumen = _calcular_resumen_comunidad(mensajes, len(interacciones))

    logger.info(
        "analisis_cognitivo_completado",
        run_id=run_id,
        request_id=request_id,
        proveedor=proveedor,
        total_procesados=len(mensajes),
        latencia_total_ms=round(total_seg * 1000, 2),
        latencia_promedio_por_registro_ms=round(total_seg / len(mensajes) * 1000, 2),
        degradados=resumen.registros_rechazados,
        tokens_cacheados=tokens_cacheados,
    )
    return AnalisisCognitivo(
        run_id=run_id,
        request_id=request_id,
        resumen_comunidad=resumen,
        mensajes_analizados=mensajes,
    )
