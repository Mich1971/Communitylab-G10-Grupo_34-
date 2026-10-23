"""Agente de An├ílisis Cognitivo (ai_modules/cognitive_analyzer.py) ÔÇö Tarea S1-1 / R2.

Transforma `List[InteraccionValidada]` en el Contrato 2 (`AnalisisCognitivo`).
Es una funci├│n transformadora sin estado: no importa LangGraph ni
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
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.exceptions import UnexpectedModelBehavior

from ai_modules.multiprovider_agent import CognitiveAgentFactory
from ai_modules.prompts import SYSTEM_PROMPT, version_de_prompt
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadatosEjecucion,
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
TIMEOUT_INFERENCIA_DEFECTO_SEG = 30.0
MAX_CHARS_TEXTO = 2000
FRACCION_INICIAL_TRUNCADO = 0.2

# Marcador estable: el resumen y el orquestador identifican registros degradados
# por este prefijo en vez de depender de texto libre incrustado en la l├│gica.
PREFIJO_DEGRADACION = "Degradaci├│n por error de servicio tras reintentos"

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
# CORRECCI├ôN (Riesgo 5): Se elimin├│ el punto '.' de la clase de separadores y se
# a├▒adieron lookarounds negativos para evitar enmascarar direcciones IPv4
# (ej. 10.0.10.245), versiones sem├ínticas o timestamps ISO-8601 en logs t├®cnicos.
_RE_TELEFONO = re.compile(r"(?<![\w.])\+?\d[\d\s()-]{7,}\d(?![\w.])")


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
    """├Ünico punto de lectura de enteros de entorno del m├│dulo; valores inv├ílidos usan el defecto."""
    try:
        valor = int(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto
    return valor if valor >= 1 else defecto


def _leer_flotante_env(nombre: str, defecto: float) -> float:
    """Lectura segura de flotantes positivos desde variables de entorno (p. ej. timeouts)."""
    try:
        valor = float(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto
    return valor if valor > 0.0 else defecto


def _resolver_proveedor(provider: str | None) -> str:
    return (provider or os.environ.get("LLM_PROVIDER") or PROVEEDOR_POR_DEFECTO).lower()


def _fusionar_model_settings(agent: Any, extra_settings: dict[str, Any]) -> dict[str, Any]:
    """CORRECCI├ôN (Riesgo 1): Preserva los `model_settings` base del agente
    (como `anthropic_cache_instructions`) al inyectar ajustes por ejecuci├│n
    (p. ej. `temperature=0.0` en reintentos de validaci├│n).
    """
    base = getattr(agent, "model_settings", None)
    if isinstance(base, dict):
        return {**base, **extra_settings}
    return dict(extra_settings)


def _crear_mensaje_degradado(source_id: str, error: Exception) -> MensajeAnalizado:
    """Fallback sem├íntico: puntaje 0, no publicable, `source_id` intacto.

    Marca `requiere_soporte=True` y `categoria_enrutamiento="Dificultad"`
    para que cualquier degradaci├│n (parcial o total) quede localizable por
    `source_id` en `ResumenComunidad.alertas_soporte`.

    Solo se registra el tipo de excepci├│n: el mensaje original podr├¡a contener
    credenciales o fragmentos del prompt (M09).
    """
    return MensajeAnalizado(
        source_id=source_id,
        sentimiento="Neutro",
        categoria_enrutamiento="Dificultad",
        regla_aplicada="Degradaci├│n elegante por indisponibilidad de inferencia",
        temas=["Sin_Clasificar"],
        entidades_relevantes=[],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=0, utilidad_comunitaria=0, claridad_contexto=0
        ),
        motivo_seleccion=f"{PREFIJO_DEGRADACION}: {type(error).__name__}",
        requiere_soporte=True,
        apto_para_publicacion=False,
    )


@dataclass(frozen=True)
class _Uso:
    """Tokens de una sola inferencia. Una inferencia degradada no aporta uso."""

    cacheados: int = 0
    entrada: int = 0
    salida: int = 0


class TelemetriaAnalisis(BaseModel):
    """Telemetr├¡a de una ejecuci├│n de `analizar_lote_cognitivo_con_telemetria`.

    No es un contrato can├│nico: es un modelo interno de `ai_modules/`. Lo
    consume el orquestador para componer `MetadatosEjecucion` (Contrato 3)
    sin modificar `esquemas.py`.

    Alcance de las cifras:
    - `latencia_ms` es el tiempo de pared del lote completo. Las inferencias
      corren en paralelo, as├¡ que NO es la suma de las latencias individuales.
    - `tokens_*` suman solo las respuestas que el proveedor devolvi├│ con
      ├®xito. Los intentos que terminaron en excepci├│n no informan uso, as├¡
      que en lotes con muchos reintentos el consumo real puede ser mayor.
    """

    proveedor: str
    modelo: str
    prompt_version: str
    latencia_ms: int = Field(..., ge=0)
    tokens_entrada: int = Field(..., ge=0)
    tokens_salida: int = Field(..., ge=0)
    tokens_cacheados: int = Field(..., ge=0)
    total_procesados: int = Field(..., ge=1)
    degradados: int = Field(..., ge=0)

    @property
    def tokens_totales(self) -> int:
        return self.tokens_entrada + self.tokens_salida

    @property
    def caida_total_llm(self) -> bool:
        """Indica al orquestador si el 100% del lote se degrad├│ por fallo del LLM."""
        return self.degradados >= self.total_procesados

    def a_metadatos_ejecucion(self) -> MetadatosEjecucion:
        """Conversi├│n directa a los cuatro campos del Contrato 3."""
        return MetadatosEjecucion(
            modelo=self.modelo,
            prompt_version=self.prompt_version,
            latencia_ms=self.latencia_ms,
            tokens_totales=self.tokens_totales,
        )


def _nombre_modelo(agent: Any, proveedor: str) -> str:
    nombre = getattr(getattr(agent, "model", None), "model_name", None)
    return nombre if isinstance(nombre, str) and nombre else f"{proveedor}:desconocido"


def _preparar_entrada(interaccion: InteraccionValidada) -> str:
    texto = _truncar_texto_semantico(_enmascarar_pii(interaccion.texto))
    datos = interaccion.model_copy(update={"texto": texto}).model_dump(mode="json")
    return json.dumps(datos, ensure_ascii=False)


def _espera_backoff(intento: int) -> float:
    base = min(BACKOFF_MAX_SEG, BACKOFF_BASE_SEG * 2**intento)
    return max(0.0, base + random.uniform(-0.5, 0.5))


async def _ejecutar_inferencia_con_resiliencia(
    agent: Any,
    interaccion: InteraccionValidada,
    proveedor: str,
    max_reintentos: int,
    timeout_seg: float = TIMEOUT_INFERENCIA_DEFECTO_SEG,
) -> tuple[MensajeAnalizado, _Uso]:
    """Devuelve (mensaje, uso de tokens). Nunca propaga errores del proveedor."""
    entrada = _preparar_entrada(interaccion)
    fallos_validacion = 0
    ultimo_error: Exception = RuntimeError("sin_intentos")

    for intento in range(max_reintentos + 1):
        # CORRECCI├ôN (Riesgo 1): Si hubo fallo de esquema, forzamos temperature=0.0
        # fusion├índolo con los model_settings base del agente para no perder el cach├®.
        settings = (
            _fusionar_model_settings(agent, {"temperature": 0.0})
            if fallos_validacion
            else None
        )
        try:
            # CORRECCI├ôN (Riesgo 4): Timeout expl├¡cito por inferencia para evitar
            # que un socket colgado retenga indefinidamente el sem├íforo en el runner.
            resultado = await asyncio.wait_for(
                agent.run(entrada, model_settings=settings),
                timeout=timeout_seg,
            )
            mensaje: MensajeAnalizado = resultado.output
            # Invariante de trazabilidad: el LLM no decide el source_id.
            mensaje = mensaje.model_copy(update={"source_id": interaccion.id})
            # `AgentRunResult.usage` es propiedad en PydanticAI 2.x y m├®todo en 1.x:
            # `read_usage` acepta ambas (llamar siempre `usage()` degradaba TODO).
            uso_crudo = CognitiveAgentFactory.read_usage(resultado)
            entrada_tok, salida_tok = CognitiveAgentFactory.extract_token_counts(uso_crudo)
            uso = _Uso(
                cacheados=CognitiveAgentFactory.extract_cached_tokens(proveedor, uso_crudo),
                entrada=entrada_tok,
                salida=salida_tok,
            )
            return mensaje, uso
        except (ValidationError, UnexpectedModelBehavior) as exc:
            ultimo_error = exc
            fallos_validacion += 1
            if fallos_validacion > REINTENTOS_VALIDACION:
                break
        except Exception as exc:  # noqa: BLE001 ÔÇö incluye TimeoutError y errores de red
            ultimo_error = exc
            if intento < max_reintentos:
                await asyncio.sleep(_espera_backoff(intento))

    logger.warning(
        "inferencia_degradada",
        source_id=interaccion.id,
        error_tipo=type(ultimo_error).__name__,
    )
    return _crear_mensaje_degradado(interaccion.id, ultimo_error), _Uso()


def es_mensaje_degradado(m: MensajeAnalizado) -> bool:
    """Verifica si un `MensajeAnalizado` proviene de una degradaci├│n elegante.

    Se expone como funci├│n p├║blica para que `orchestration/pipeline_runner.py`
    pueda inspeccionar si existen mensajes genuinamente analizados antes de
    invocar al generador de copys, sin depender ├║nicamente de `registros_validos`.
    """
    return m.puntuacion_relevancia.total == 0 and m.motivo_seleccion.startswith(
        PREFIJO_DEGRADACION
    )


# Alias privado retrocompatible para llamadas o tests internos existentes
_es_degradado = es_mensaje_degradado


def tiene_analisis_utilizable(analisis: AnalisisCognitivo) -> bool:
    """CORRECCI├ôN (Riesgo 3): Devuelve `True` si al menos un mensaje del lote
    fue analizado exitosamente por el LLM (no degradado).

    Permite que `pipeline_runner.py` detecte ca├¡das totales del proveedor LLM
    incluso cuando `ResumenComunidad.registros_validos` vale 1 por restricci├│n
    del esquema Pydantic (`Field(..., ge=1)`).
    """
    return any(not es_mensaje_degradado(m) for m in analisis.mensajes_analizados)


def _calcular_resumen_comunidad(
    mensajes: list[MensajeAnalizado], total_recibidos_en_lote: int
) -> ResumenComunidad:
    """Agregaci├│n determinista; el LLM nunca calcula estad├¡sticas del lote."""
    degradados = sum(es_mensaje_degradado(m) for m in mensajes)
    conteo: dict[str, int] = dict.fromkeys(SENTIMIENTOS_OFICIALES, 0)
    for m in mensajes:
        conteo[m.sentimiento] += 1

    if mensajes:
        maximo = max(conteo.values())
        categorias_en_maximo = [k for k, v in conteo.items() if v == maximo]
        moda = categorias_en_maximo[0] if len(categorias_en_maximo) == 1 else "Mixto"
    else:
        moda = "Neutro"

    temas = [t for m in mensajes for t in m.temas]
    temas_top = [t for t, _ in Counter(temas).most_common(5)] or ["General"]
    alertas = [f"[{m.source_id}] {m.motivo_seleccion}" for m in mensajes if m.requiere_soporte]

    total_procesados = max(1, total_recibidos_en_lote)
    validos_reales = len(mensajes) - degradados

    if validos_reales < 1:
        # CORRECCI├ôN (Riesgo 3): El contrato en `esquemas.py` exige `registros_validos >= 1`.
        # Para no violar la invariante aritm├®tica del dashboard (validos + rechazados == total)
        # cuando ocurre una ca├¡da total del LLM, se ajusta `rechazados_reportados` para que
        # nunca exceda `total_procesados - 1`, dejando constancia expl├¡cita en `alertas_soporte`.
        alertas.append(
            "ALERTA CR├ìTICA: Todos los registros del lote fueron degradados por indisponibilidad del LLM"
        )
        validos_reportados = 1
        rechazados_reportados = max(0, total_procesados - 1)
    else:
        validos_reportados = validos_reales
        rechazados_reportados = degradados

    return ResumenComunidad(
        total_interacciones_procesadas=total_procesados,
        registros_validos=validos_reportados,
        registros_rechazados=rechazados_reportados,
        sentimiento_predominante=moda,
        distribucion_sentimiento=conteo,
        temas_principales=temas_top,
        alertas_soporte=alertas,
    )


async def analizar_lote_cognitivo_con_telemetria(
    interacciones: list[InteraccionValidada],
    run_id: str,
    request_id: str,
    provider: str | None = None,
    max_concurrency: int | None = None,
    system_prompt: str | None = None,
    timeout_seg: float | None = None,
) -> tuple[AnalisisCognitivo, TelemetriaAnalisis]:
    """Analiza un lote de interacciones ya validadas y devuelve `(AnalisisCognitivo, TelemetriaAnalisis)`."""
    if not interacciones:
        raise ValueError("analizar_lote_cognitivo requiere al menos una interacci├│n validada")

    proveedor = _resolver_proveedor(provider)
    concurrencia = max_concurrency or _leer_entero_env(
        "COGNITIVE_ANALYZER_CONCURRENCY", CONCURRENCIA_DEFECTO
    )
    max_reintentos = _leer_entero_env("LLM_MAX_RETRIES", MAX_REINTENTOS_DEFECTO)
    timeout_efectivo = timeout_seg or _leer_flotante_env(
        "LLM_INFERENCE_TIMEOUT_SEC", TIMEOUT_INFERENCIA_DEFECTO_SEG
    )

    agent = CognitiveAgentFactory.create_agent(
        proveedor, system_prompt or SYSTEM_PROMPT, MensajeAnalizado
    )
    semaforo = asyncio.Semaphore(concurrencia)

    async def _procesar(i: InteraccionValidada) -> tuple[MensajeAnalizado, _Uso]:
        async with semaforo:
            return await _ejecutar_inferencia_con_resiliencia(
                agent, i, proveedor, max_reintentos, timeout_seg=timeout_efectivo
            )

    inicio = time.perf_counter()
    resultados = await asyncio.gather(*(_procesar(i) for i in interacciones))
    total_seg = time.perf_counter() - inicio

    mensajes = [m for m, _ in resultados]
    degradados_reales = sum(es_mensaje_degradado(m) for m in mensajes)
    tokens_cacheados = sum(u.cacheados for _, u in resultados)
    tokens_entrada = sum(u.entrada for _, u in resultados)
    tokens_salida = sum(u.salida for _, u in resultados)

    resumen = _calcular_resumen_comunidad(mensajes, len(interacciones))
    telemetria = TelemetriaAnalisis(
        proveedor=proveedor,
        modelo=_nombre_modelo(agent, proveedor),
        prompt_version=version_de_prompt(system_prompt or SYSTEM_PROMPT),
        latencia_ms=round(total_seg * 1000),
        tokens_entrada=tokens_entrada,
        tokens_salida=tokens_salida,
        tokens_cacheados=tokens_cacheados,
        total_procesados=len(mensajes),
        degradados=degradados_reales,
    )

    logger.info(
        "analisis_cognitivo_completado",
        run_id=run_id,
        request_id=request_id,
        proveedor=proveedor,
        total_procesados=len(mensajes),
        latencia_total_ms=round(total_seg * 1000, 2),
        latencia_promedio_por_registro_ms=round(total_seg / len(mensajes) * 1000, 2),
        degradados=degradados_reales,
        caida_total_llm=telemetria.caida_total_llm,
        tokens_cacheados=tokens_cacheados,
        tokens_entrada=tokens_entrada,
        tokens_salida=tokens_salida,
        modelo=telemetria.modelo,
        prompt_version=telemetria.prompt_version,
    )
    analisis = AnalisisCognitivo(
        run_id=run_id,
        request_id=request_id,
        resumen_comunidad=resumen,
        mensajes_analizados=mensajes,
    )
    return analisis, telemetria


async def analizar_lote_cognitivo(
    interacciones: list[InteraccionValidada],
    run_id: str,
    request_id: str,
    provider: str | None = None,
    max_concurrency: int | None = None,
    system_prompt: str | None = None,
    timeout_seg: float | None = None,
) -> AnalisisCognitivo:
    """Analiza un lote de interacciones ya validadas y devuelve el Contrato 2."""
    analisis, _ = await analizar_lote_cognitivo_con_telemetria(
        interacciones,
        run_id,
        request_id,
        provider=provider,
        max_concurrency=max_concurrency,
        system_prompt=system_prompt,
        timeout_seg=timeout_seg,
    )
    return analisis
