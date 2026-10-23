"""Generación de activos de marketing compatible con pipeline_runner.

Incluye trazabilidad determinista, privacidad de casos no publicables,
selección editorial y degradación segura a plantillas si falla el LLM.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ai_modules.multiprovider_agent import CognitiveAgentFactory, resolver_modelo
from esquemas import (
    InteraccionValidada,
    MensajeAnalizado,
    MetadatosEjecucion,
    PostLinkedIn,
    ResumenSemanal,
)

logger = structlog.get_logger("ai_modules.generador")
VERSION_PROMPTS_GENERACION = "generacion-1.0.1"


class TelemetriaGeneracion(BaseModel):
    proveedor: str
    modelo: str
    prompt_version: str
    latencia_ms: int = Field(ge=0)
    tokens_entrada: int = Field(default=0, ge=0)
    tokens_salida: int = Field(default=0, ge=0)
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


def _seleccionar_fuente(
    analisis: Any,
    interacciones: list[InteraccionValidada],
    enfoque: str,
) -> tuple[InteraccionValidada, MensajeAnalizado]:
    """Prioriza coincidencia de enfoque y, después, puntuación de relevancia."""
    por_id = {interaccion.id: interaccion for interaccion in interacciones}
    candidatos = [
        mensaje
        for mensaje in analisis.mensajes_analizados
        if mensaje.apto_para_publicacion and mensaje.source_id in por_id
    ]
    if not candidatos:
        raise ValueError("No hay mensajes publicables con interacción original disponible.")

    mensaje = max(
        candidatos,
        key=lambda item: (
            int(item.categoria_enrutamiento == enfoque),
            item.puntuacion_relevancia.total,
        ),
    )
    return por_id[mensaje.source_id], mensaje


def _registros_resumen(
    interacciones: list[InteraccionValidada], analisis: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    """Prepara el contexto del resumen sin filtrar texto de casos no publicables."""
    por_id = {interaccion.id: interaccion for interaccion in interacciones}
    registros: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for mensaje in analisis.mensajes_analizados:
        interaccion = por_id.get(mensaje.source_id)
        if interaccion is None:
            continue
        registro: dict[str, Any] = {
            "source_id": mensaje.source_id,
            "sentimiento": mensaje.sentimiento,
            "categoria": mensaje.categoria_enrutamiento,
            "temas": mensaje.temas,
            "apto_para_publicacion": mensaje.apto_para_publicacion,
        }
        if mensaje.apto_para_publicacion:
            registro["texto"] = interaccion.texto
        registros.append(registro)
        source_ids.append(mensaje.source_id)
    if not registros:
        raise ValueError("No hay interacciones vinculadas para generar el resumen semanal.")
    return registros, source_ids


def _texto_minimo(texto: str, minimo: int = 20) -> str:
    """Completa una longitud mínima con una descripción neutral, sin crear hechos."""
    resultado = texto.strip()
    if len(resultado) >= minimo:
        return resultado
    sufijo = " Contexto comunitario registrado."
    resultado = f"{resultado}{sufijo}".strip()
    while len(resultado) < minimo:
        resultado += " Registro válido."
    return resultado


def _activos_degradados(
    interaccion_post: InteraccionValidada,
    mensaje_post: MensajeAnalizado,
    registros: list[dict[str, Any]],
    ids_resumen: list[str],
) -> tuple[PostLinkedIn, ResumenSemanal]:
    """Plantillas seguras: solo usan hechos disponibles y no reproducen quejas privadas."""
    temas_post = [str(tema) for tema in mensaje_post.temas if str(tema).strip()]
    tema_post = temas_post[0] if temas_post else "comunidad tecnológica"
    titulo_post = f"Comunidad: {tema_post}"[:200]
    cuerpo_post = _texto_minimo(
        f"{interaccion_post.texto.strip()} Contexto documentado: {', '.join(temas_post) or tema_post}. "
        f"Canal de origen: {interaccion_post.canal}."
    )
    post = PostLinkedIn(
        titulo=titulo_post,
        cuerpo=cuerpo_post,
        hashtags=[f"#{''.join(c for c in tema.title() if c.isalnum())}" for tema in temas_post[:5]],
        source_ids=[mensaje_post.source_id],
    )

    temas: list[str] = []
    categorias: list[str] = []
    sentimientos: list[str] = []
    for registro in registros:
        for tema in registro.get("temas", []):
            if tema and str(tema) not in temas:
                temas.append(str(tema))
        categoria = registro.get("categoria")
        sentimiento = registro.get("sentimiento")
        if categoria and str(categoria) not in categorias:
            categorias.append(str(categoria))
        if sentimiento and str(sentimiento) not in sentimientos:
            sentimientos.append(str(sentimiento))
    resumen_texto = (
        "Temas registrados: " + (", ".join(temas) or "sin tema detallado") + ". "
        "Categorías observadas: " + (", ".join(categorias) or "sin categoría detallada") + ". "
        "Sentimientos observados: " + (", ".join(sentimientos) or "sin clasificación disponible") + "."
    )
    resumen = ResumenSemanal(
        seccion="Actividad comunitaria",
        titular="Resumen semanal de la comunidad",
        resumen=_texto_minimo(resumen_texto),
        source_ids=ids_resumen,
    )
    return post, resumen


def _tokens(resultado: Any, proveedor: str) -> tuple[int, int, int]:
    """Lee tokens de forma tolerante: un campo ausente no debe tumbar el activo."""
    try:
        uso = CognitiveAgentFactory.read_usage(resultado)
        entrada, salida = CognitiveAgentFactory.extract_token_counts(uso)
        cacheados = CognitiveAgentFactory.extract_cached_tokens(proveedor, uso)
        return max(0, int(entrada or 0)), max(0, int(salida or 0)), max(0, int(cacheados or 0))
    except Exception as exc:  # telemetría es secundaria a producir activos seguros
        logger.warning("telemetria_tokens_no_disponible", error_tipo=type(exc).__name__)
        return 0, 0, 0


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

    # Definir ambos prompts ANTES de calcular su huella; evita UnboundLocalError.
    system_post = (
        "Eres redactor de una comunidad tecnológica. Redacta una propuesta profesional "
        "para LinkedIn usando exclusivamente los datos proporcionados. No inventes cifras, "
        "logros ni resultados. No afirmes que está aprobada o publicada."
    )
    system_resumen = (
        "Eres editor de una comunidad tecnológica. Resume la actividad semanal para revisión "
        "humana, sin inventar hechos ni reproducir textos individuales de casos no publicables."
    )
    huella = hashlib.sha256(f"{system_post}\n---\n{system_resumen}".encode("utf-8")).hexdigest()[:12]
    version = f"{VERSION_PROMPTS_GENERACION}+sha256:{huella}"

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
        "no publicables, pero no revelar ni reproducir su texto individual. Devuelve ResumenSemanal."
    )

    degradado = False
    entrada = salida = cacheados = 0
    try:
        agent_post = CognitiveAgentFactory.create_agent(proveedor, system_post, PostLinkedIn)
        agent_resumen = CognitiveAgentFactory.create_agent(proveedor, system_resumen, ResumenSemanal)

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
            "cuerpo": _texto_minimo(post_generado.cuerpo),
            "source_ids": [mensaje_post.source_id],
        })
        resumen = ResumenSemanal.model_validate({
            **resumen_generado.model_dump(),
            "resumen": _texto_minimo(resumen_generado.resumen),
            "source_ids": ids_resumen,
        })
        entrada_post, salida_post, cache_post = _tokens(resultado_post, proveedor)
        entrada_resumen, salida_resumen, cache_resumen = _tokens(resultado_resumen, proveedor)
        entrada = entrada_post + entrada_resumen
        salida = salida_post + salida_resumen
        cacheados = cache_post + cache_resumen
    except Exception as exc:
        degradado = True
        logger.warning(
            "generacion_ia_degradada_a_plantilla",
            run_id=run_id,
            request_id=request_id,
            enfoque_editorial=enfoque_editorial,
            error_tipo=type(exc).__name__,
        )
        post, resumen = _activos_degradados(
            interaccion_post, mensaje_post, registros, ids_resumen
        )

    telemetria = TelemetriaGeneracion(
        proveedor=proveedor,
        modelo=modelo,
        prompt_version=version,
        latencia_ms=round((time.perf_counter() - inicio) * 1000),
        tokens_entrada=entrada,
        tokens_salida=salida,
        tokens_cacheados=cacheados,
    )
    logger.info(
        "generacion_activos_completada",
        run_id=run_id,
        request_id=request_id,
        proveedor=proveedor,
        modelo=modelo,
        prompt_version=version,
        enfoque_editorial=enfoque_editorial,
        temperatura_cero=temperatura_cero,
        latencia_ms=telemetria.latencia_ms,
        tokens_entrada=telemetria.tokens_entrada,
        tokens_salida=telemetria.tokens_salida,
        tokens_totales=telemetria.tokens_totales,
        tokens_cacheados=telemetria.tokens_cacheados,
        post_source_ids=post.source_ids,
        resumen_source_ids=resumen.source_ids,
        degradado=degradado,
    )
    return post, resumen, telemetria
