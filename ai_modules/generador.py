"""Generador compatible con orchestration.pipeline_runner."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ai_modules.multiprovider_agent import CognitiveAgentFactory, resolver_modelo
from esquemas import InteraccionValidada, MensajeAnalizado, MetadatosEjecucion, PostLinkedIn, ResumenSemanal

logger = structlog.get_logger("ai_modules.generador")
VERSION_PROMPTS_GENERACION = "generacion-1.0.0"


class TelemetriaGeneracion(BaseModel):
    proveedor: str
    modelo: str
    prompt_version: str
    latencia_ms: int = Field(ge=0)
    tokens_entrada: int = Field(ge=0)
    tokens_salida: int = Field(ge=0)
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


def _proveedor_activo() -> str:
    proveedor = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    if proveedor not in {"openai", "anthropic", "gemini"}:
        raise ValueError(f"Proveedor LLM no soportado: {proveedor!r}")
    return proveedor


def _ajustes(agent: Any, temperatura_cero: bool) -> dict[str, Any] | None:
    if not temperatura_cero:
        return None
    base = getattr(agent, "model_settings", None)
    ajustes = dict(base) if isinstance(base, dict) else {}
    ajustes["temperature"] = 0.0
    return ajustes


def _seleccionar_fuente(analisis: Any, interacciones: list[InteraccionValidada], enfoque: str):
    por_id = {i.id: i for i in interacciones}
    candidatos = [
        m for m in analisis.mensajes_analizados
        if m.apto_para_publicacion and m.source_id in por_id
    ]
    if not candidatos:
        raise ValueError("No hay mensajes publicables con interacción original disponible.")

    def prioridad(m: MensajeAnalizado) -> tuple[int, int]:
        return (int(m.categoria_enrutamiento == enfoque), m.puntuacion_relevancia.total)

    mensaje = max(candidatos, key=prioridad)
    return por_id[mensaje.source_id], mensaje


def _registros_resumen(interacciones: list[InteraccionValidada], analisis: Any):
    por_id = {i.id: i for i in interacciones}
    registros: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for m in analisis.mensajes_analizados:
        interaccion = por_id.get(m.source_id)
        if interaccion is None:
            continue
        registro: dict[str, Any] = {
            "source_id": m.source_id,
            "sentimiento": m.sentimiento,
            "categoria": m.categoria_enrutamiento,
            "temas": m.temas,
            "apto_para_publicacion": m.apto_para_publicacion,
        }
        # No enviar texto original de casos no publicables al LLM.
        if m.apto_para_publicacion:
            registro["texto"] = interaccion.texto
        registros.append(registro)
        source_ids.append(m.source_id)
    if not registros:
        raise ValueError("No hay interacciones vinculadas para generar el resumen semanal.")
    return registros, source_ids


async def generar_activos_marketing(
    analisis_cognitivo: Any,
    interacciones_validas: list[InteraccionValidada],
    enfoque_editorial: str,
    run_id: str,
    request_id: str,
    temperatura_cero: bool = False,
) -> tuple[PostLinkedIn, ResumenSemanal, TelemetriaGeneracion]:
    inicio = time.perf_counter()
    proveedor = _proveedor_activo()
    modelo = resolver_modelo(proveedor)

    interaccion_post, mensaje_post = _seleccionar_fuente(
        analisis_cognitivo, interacciones_validas, enfoque_editorial
    )
    registros, ids_resumen = _registros_resumen(interacciones_validas, analisis_cognitivo)

    system_post = (
        "Eres redactor de una comunidad tecnológica. Redacta una propuesta profesional "
        "para LinkedIn usando exclusivamente los datos proporcionados. No inventes cifras, "
        "logros ni resultados. No afirmes que está aprobada o publicada."
    )
    version = VERSION_PROMPTS_GENERACION

    system_resumen = (
        "Eres editor de una comunidad tecnológica. Resume la actividad semanal para revisión "
        "humana, sin inventar hechos ni reproducir textos individuales de casos no publicables."
    )
    agent_post = CognitiveAgentFactory.create_agent(proveedor, system_post, PostLinkedIn)
    agent_resumen = CognitiveAgentFactory.create_agent(proveedor, system_resumen, ResumenSemanal)

    prompt_post = (
        f"run_id={run_id}; request_id={request_id}; enfoque={enfoque_editorial}\n"
        f"source_id autorizado={mensaje_post.source_id}\n"
        f"categoría={mensaje_post.categoria_enrutamiento}\n"
        f"temas={mensaje_post.temas}; entidades={mensaje_post.entidades_relevantes}\n"
        f"texto de origen={interaccion_post.texto}\n"
        "El título debe tener al menos 5 caracteres, el cuerpo al menos 20 y los hashtags "
        "deben relacionarse con los temas. Devuelve el contrato PostLinkedIn."
    )
    prompt_resumen = (
        f"run_id={run_id}; request_id={request_id}; enfoque={enfoque_editorial}\n"
        f"Registros de la semana: {registros}\n"
        "Genera titular y resumen de al menos 20 caracteres. Puedes sintetizar temas de casos "
        "no publicables, pero no revelar su texto individual. Devuelve ResumenSemanal."
    )

    async def ejecutar(agent: Any, prompt: str):
        settings = _ajustes(agent, temperatura_cero)
        if settings is None:
            return await agent.run(prompt)
        return await agent.run(prompt, model_settings=settings)

    resultado_post, resultado_resumen = await asyncio.gather(
        ejecutar(agent_post, prompt_post),
        ejecutar(agent_resumen, prompt_resumen),
    )

    post_generado = resultado_post.output
    resumen_generado = resultado_resumen.output
    post = PostLinkedIn.model_validate({
        **post_generado.model_dump(),
        "source_ids": [mensaje_post.source_id],
    })
    resumen = ResumenSemanal.model_validate({
        **resumen_generado.model_dump(),
        "source_ids": ids_resumen,
    })

    uso_post = CognitiveAgentFactory.read_usage(resultado_post)
    uso_resumen = CognitiveAgentFactory.read_usage(resultado_resumen)
    entrada_post, salida_post = CognitiveAgentFactory.extract_token_counts(uso_post)
    entrada_resumen, salida_resumen = CognitiveAgentFactory.extract_token_counts(uso_resumen)

    telemetria = TelemetriaGeneracion(
        proveedor=proveedor,
        modelo=modelo,
        prompt_version=version,
        latencia_ms=round((time.perf_counter() - inicio) * 1000),
        tokens_entrada=entrada_post + entrada_resumen,
        tokens_salida=salida_post + salida_resumen,
        tokens_cacheados=(
            CognitiveAgentFactory.extract_cached_tokens(proveedor, uso_post)
            + CognitiveAgentFactory.extract_cached_tokens(proveedor, uso_resumen)
        ),
    )
    logger.info(
        "activos_marketing_generados",
        run_id=run_id,
        request_id=request_id,
        proveedor=proveedor,
        modelo=modelo,
        enfoque_editorial=enfoque_editorial,
        source_id_post=mensaje_post.source_id,
        source_ids_resumen=len(resumen.source_ids),
        latencia_ms=telemetria.latencia_ms,
        tokens_totales=telemetria.tokens_totales,
        temperatura_cero=temperatura_cero,
    )
    return post, resumen, telemetria
