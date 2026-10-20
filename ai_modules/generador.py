"""Generador de activos de marketing (ai_modules/generador.py) — spec v3.1, Nodo 3.

Transforma el Contrato 2 (`AnalisisCognitivo`) y los textos originales de los
mensajes aptos en los dos formatos base del Contrato 3: `PostLinkedIn` y
`ResumenSemanal`. Ambos se generan SIEMPRE y en paralelo (Regla M01): el
`enfoque_editorial` solo orienta el tono, nunca bifurca el formato.

Política de cero hechos inventados: el prompt prohíbe afirmar algo que no esté
en el material y exige citar `source_ids` reales. La verificación definitiva de
esos identificadores la realiza el orquestador (sección 6.3 de la spec), que
rechaza el activo y reintenta con temperatura cero si se viola la invariante.

Es una función transformadora sin estado: no importa LangGraph, SQLite ni OCI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ai_modules.cognitive_analyzer import (
    _enmascarar_pii,
    _espera_backoff,
    _fusionar_model_settings,
    _leer_entero_env,
    _leer_flotante_env,
    _nombre_modelo,
    _resolver_proveedor,
    _truncar_texto_semantico,
)
from ai_modules.multiprovider_agent import CognitiveAgentFactory
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MetadatosEjecucion,
    PostLinkedIn,
    ResumenSemanal,
)

logger = structlog.get_logger("ai_modules.generador")

MAX_REINTENTOS_DEFECTO = 3
TIMEOUT_GENERACION_DEFECTO_SEG = 60.0
MAX_CHARS_TEXTO_MATERIAL = 1200

ENFOQUES_EDITORIALES = ("Logro", "Duda", "Dificultad")

_TONO_POR_ENFOQUE = {
    "Logro": (
        "Tono celebratorio y motivador: destaca el avance y reconoce a la comunidad "
        "sin exagerar ni añadir resultados que no estén en el material."
    ),
    "Duda": (
        "Tono didáctico y aclaratorio: convierte las preguntas frecuentes en orientación "
        "útil, sin responder con información que no esté en el material."
    ),
    "Dificultad": (
        "Tono empático y orientado a soluciones: reconoce el obstáculo con respeto y "
        "enfatiza el apoyo disponible, sin dramatizar ni inventar causas."
    ),
}

_REGLAS_COMUNES = """\
REGLAS INNEGOCIABLES
1. CERO HECHOS INVENTADOS: usa únicamente información presente en el MATERIAL.
   No inventes cifras, nombres, fechas, herramientas ni resultados.
2. TRAZABILIDAD: en `source_ids` incluye solo identificadores que aparezcan en
   `source_ids_permitidos` y que respalden lo que afirmas. Nunca dejes la lista vacía.
3. PRIVACIDAD: no reproduzcas correos, teléfonos, credenciales ni datos personales.
   Evita citar a los autores por su nombre.
4. IDIOMA: español neutro, claro y profesional.
5. El tono lo marca `enfoque_editorial`; el formato de salida no cambia.
"""

SYSTEM_PROMPT_POST_LINKEDIN = (
    "Eres el redactor de comunidad de CommunityLab. Escribes UN post de LinkedIn "
    "a partir de mensajes reales de la comunidad que ya fueron aprobados para difusión.\n\n"
    + _REGLAS_COMUNES
    + "\nFORMATO (esquema `PostLinkedIn`):\n"
    "- `titulo`: gancho breve (mínimo 5 caracteres).\n"
    "- `canal_recomendado`: \"LinkedIn Oficial\".\n"
    "- `cuerpo`: 2 a 4 párrafos cortos (mínimo 20 caracteres).\n"
    "- `hashtags`: de 3 a 6 hashtags relevantes.\n"
    "- `source_ids`: mensajes que respaldan el post.\n"
)

SYSTEM_PROMPT_RESUMEN_SEMANAL = (
    "Eres el editor del boletín semanal de CommunityLab. Sintetizas lo más relevante "
    "de la semana a partir de mensajes reales de la comunidad.\n\n"
    + _REGLAS_COMUNES
    + "\nFORMATO (esquema `ResumenSemanal`):\n"
    "- `seccion`: nombre de la sección del boletín (por ejemplo \"Destacados de la semana\").\n"
    "- `titular`: titular informativo (mínimo 5 caracteres).\n"
    "- `resumen`: síntesis de un párrafo (mínimo 20 caracteres).\n"
    "- `source_ids`: mensajes que respaldan la síntesis.\n"
)

# Versión semántica a incrementar a mano al cambiar los prompts; la huella del
# contenido evita que el número quede desactualizado en silencio (misma
# convención que `ai_modules/prompts/analyzer.py`).
PROMPT_VERSION_GENERADOR_BASE = "1.0.0"
PROMPT_VERSION_GENERADOR = (
    f"generador-{PROMPT_VERSION_GENERADOR_BASE}+"
    + hashlib.sha256(
        (SYSTEM_PROMPT_POST_LINKEDIN + SYSTEM_PROMPT_RESUMEN_SEMANAL).encode("utf-8")
    ).hexdigest()[:8]
)


class GeneracionNoDisponibleError(RuntimeError):
    """El proveedor LLM no pudo generar un activo tras agotar los reintentos."""


class TelemetriaGeneracion(BaseModel):
    """Telemetría de una ejecución de `generar_activos_marketing`.

    No es un contrato canónico: es un modelo interno de `ai_modules/`, gemelo de
    `TelemetriaAnalisis`, que el orquestador usa para componer `MetadatosEjecucion`.
    """

    proveedor: str
    modelo: str
    prompt_version: str
    latencia_ms: int = Field(..., ge=0)
    tokens_entrada: int = Field(..., ge=0)
    tokens_salida: int = Field(..., ge=0)
    tokens_cacheados: int = Field(default=0, ge=0)

    @property
    def tokens_totales(self) -> int:
        return self.tokens_entrada + self.tokens_salida

    def a_metadatos_ejecucion(self) -> MetadatosEjecucion:
        return MetadatosEjecucion(
            modelo=self.modelo,
            prompt_version=self.prompt_version,
            latencia_ms=self.latencia_ms,
            tokens_totales=self.tokens_totales,
        )


def construir_material(
    analisis: AnalisisCognitivo,
    interacciones: list[InteraccionValidada],
    enfoque_editorial: str,
) -> dict[str, Any]:
    """Arma el material que ve el LLM: solo mensajes aptos, con PII enmascarada."""
    textos = {i.id: i.texto for i in interacciones}
    mensajes = []
    for m in analisis.mensajes_analizados:
        if not m.apto_para_publicacion:
            continue
        texto = textos.get(m.source_id, "")
        mensajes.append(
            {
                "source_id": m.source_id,
                "texto": _truncar_texto_semantico(_enmascarar_pii(texto), MAX_CHARS_TEXTO_MATERIAL),
                "sentimiento": m.sentimiento,
                "categoria": m.categoria_enrutamiento,
                "temas": list(m.temas),
                "relevancia": m.puntuacion_relevancia.total,
            }
        )
    resumen = analisis.resumen_comunidad
    return {
        "enfoque_editorial": enfoque_editorial,
        "instruccion_de_tono": _TONO_POR_ENFOQUE.get(enfoque_editorial, ""),
        "source_ids_permitidos": [m["source_id"] for m in mensajes],
        "contexto_lote": {
            "sentimiento_predominante": resumen.sentimiento_predominante,
            "temas_principales": list(resumen.temas_principales),
        },
        "mensajes": mensajes,
    }


async def _generar_activo(
    agent: Any,
    entrada: str,
    max_reintentos: int,
    timeout_seg: float,
    temperatura_cero: bool,
) -> tuple[Any, int, int, int]:
    """Ejecuta un agente con reintentos y backoff. Devuelve (salida, entrada, salida, cacheados)."""
    ultimo_error: Exception = RuntimeError("sin_intentos")
    for intento in range(max_reintentos + 1):
        settings = _fusionar_model_settings(agent, {"temperature": 0.0}) if temperatura_cero else None
        try:
            resultado = await asyncio.wait_for(
                agent.run(entrada, model_settings=settings), timeout=timeout_seg
            )
            uso = CognitiveAgentFactory.read_usage(resultado)
            tok_in, tok_out = CognitiveAgentFactory.extract_token_counts(uso)
            return resultado.output, tok_in, tok_out, 0
        except Exception as exc:  # noqa: BLE001 — incluye timeouts, red y validación del esquema
            ultimo_error = exc
            if intento < max_reintentos:
                await asyncio.sleep(_espera_backoff(intento))
    raise GeneracionNoDisponibleError(
        f"{type(ultimo_error).__name__}: {ultimo_error}"
    ) from ultimo_error


async def generar_activos_marketing(
    analisis: AnalisisCognitivo,
    interacciones: list[InteraccionValidada],
    enfoque_editorial: str,
    run_id: str,
    request_id: str,
    provider: str | None = None,
    temperatura_cero: bool = False,
    timeout_seg: float | None = None,
) -> tuple[PostLinkedIn, ResumenSemanal, TelemetriaGeneracion]:
    """Genera `PostLinkedIn` y `ResumenSemanal` simultáneamente (M01).

    Args:
        analisis: Contrato 2 con al menos un mensaje apto.
        interacciones: textos originales ya validados (para fundamentar el copy).
        enfoque_editorial: "Logro" | "Duda" | "Dificultad"; orienta solo el tono.
        temperatura_cero: fuerza `temperature=0.0` (reintentos tras violar una invariante).

    Raises:
        ValueError: si no hay ningún mensaje apto en el análisis.
        GeneracionNoDisponibleError: si el proveedor falla tras agotar reintentos.
    """
    material = construir_material(analisis, interacciones, enfoque_editorial)
    if not material["mensajes"]:
        raise ValueError("generar_activos_marketing requiere al menos un mensaje apto")

    proveedor = _resolver_proveedor(provider)
    max_reintentos = _leer_entero_env("LLM_MAX_RETRIES", MAX_REINTENTOS_DEFECTO)
    timeout_efectivo = timeout_seg or _leer_flotante_env(
        "LLM_INFERENCE_TIMEOUT_SEC", TIMEOUT_GENERACION_DEFECTO_SEG
    )

    agente_post = CognitiveAgentFactory.create_agent(
        proveedor, SYSTEM_PROMPT_POST_LINKEDIN, PostLinkedIn
    )
    agente_resumen = CognitiveAgentFactory.create_agent(
        proveedor, SYSTEM_PROMPT_RESUMEN_SEMANAL, ResumenSemanal
    )
    entrada = json.dumps(material, ensure_ascii=False)

    inicio = time.perf_counter()
    (post, in_a, out_a, cache_a), (resumen, in_b, out_b, cache_b) = await asyncio.gather(
        _generar_activo(agente_post, entrada, max_reintentos, timeout_efectivo, temperatura_cero),
        _generar_activo(agente_resumen, entrada, max_reintentos, timeout_efectivo, temperatura_cero),
    )
    total_seg = time.perf_counter() - inicio

    modelo = _nombre_modelo(agente_post, proveedor)
    telemetria = TelemetriaGeneracion(
        proveedor=proveedor,
        modelo=modelo,
        prompt_version=PROMPT_VERSION_GENERADOR,
        latencia_ms=round(total_seg * 1000),
        tokens_entrada=in_a + in_b,
        tokens_salida=out_a + out_b,
        tokens_cacheados=cache_a + cache_b,
    )
    logger.info(
        "activos_marketing_generados",
        run_id=run_id,
        request_id=request_id,
        proveedor=proveedor,
        modelo=modelo,
        enfoque_editorial=enfoque_editorial,
        temperatura_cero=temperatura_cero,
        latencia_ms=telemetria.latencia_ms,
        tokens_totales=telemetria.tokens_totales,
    )
    return post, resumen, telemetria
