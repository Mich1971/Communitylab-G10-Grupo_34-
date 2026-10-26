"""Generación de activos de marketing compatible con pipeline_runner.

Incluye trazabilidad determinista, privacidad de casos no publicables,
enmascaramiento de PII, timeout por inferencia, selección editorial y
degradación segura a plantillas si falla el LLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ai_modules.cognitive_analyzer import (
    TIMEOUT_INFERENCIA_DEFECTO_SEG,
    _enmascarar_pii,
    _leer_flotante_env,
)
from ai_modules.multiprovider_agent import CognitiveAgentFactory, resolver_modelo
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MensajeAnalizado,
    MetadatosEjecucion,
    PaqueteSalida,
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
    """Prepara el contexto del resumen sin filtrar texto de casos no publicables y sanitizando PII."""
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
            registro["texto"] = _enmascarar_pii(interaccion.texto)
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


# ---------------------------------------------------------------------------
# API de compatibilidad con el generador original (Contrato 3)
# ---------------------------------------------------------------------------
def buscar_interaccion_por_source_id(
    source_id: str, interacciones: list[InteraccionValidada]
) -> dict[str, Any]:
    """Busca una interacción por el identificador factual del análisis."""
    for interaccion in interacciones:
        if interaccion.id == source_id:
            return {"encontrado": True, "interaccion": interaccion}
    return {"encontrado": False, "interaccion": None}


def registrar_no_publicable(analisis: MensajeAnalizado) -> CasoNoPublicable:
    """Construye el caso para curaduría sin generar texto nuevo."""
    return CasoNoPublicable.desde_mensaje_analizado(analisis)


def generar_post_linkedin(
    interaccion: InteraccionValidada, analisis: MensajeAnalizado
) -> PostLinkedIn | None:
    """Genera un post determinista compatible con el contrato original."""
    if interaccion.id != analisis.source_id:
        raise ValueError("La interacción y el análisis no corresponden al mismo source_id")
    if not analisis.apto_para_publicacion:
        return None

    temas = [str(tema).strip() for tema in analisis.temas if str(tema).strip()]
    entidades = [
        str(entidad).strip() for entidad in analisis.entidades_relevantes if str(entidad).strip()
    ]
    if not temas and not entidades:
        return None
    tema_principal = entidades[0] if entidades else temas[0]
    texto_sanitizado = _enmascarar_pii(interaccion.texto.strip())
    cuerpo_base = (
        f"{texto_sanitizado} Contexto documentado: {', '.join(temas or entidades)}. "
        f"Canal de origen: {interaccion.canal}."
    )
    return PostLinkedIn(
        titulo=_texto_minimo(f"Tema destacado: {tema_principal}", minimo=5)[:200],
        canal_recomendado="LinkedIn Oficial",
        cuerpo=_texto_minimo(cuerpo_base),
        hashtags=[f"#{''.join(c for c in tema.title() if c.isalnum())}" for tema in temas[:5]],
        source_ids=[analisis.source_id],
    )


def generar_resumen_semanal(
    interacciones: list[InteraccionValidada], analisis_lista: list[MensajeAnalizado]
) -> tuple[ResumenSemanal, list[CasoNoPublicable]]:
    """Genera un resumen determinista; nunca copia texto no publicable."""
    source_ids: list[str] = []
    textos_publicables: list[str] = []
    temas: list[str] = []
    categorias: list[str] = []
    sentimientos: list[str] = []
    casos_no_publicables: list[CasoNoPublicable] = []
    por_id = {interaccion.id: interaccion for interaccion in interacciones}

    for analisis in analisis_lista:
        interaccion = por_id.get(analisis.source_id)
        if interaccion is None:
            # Un activo sin interacción original no debe recibir source_id.
            continue
        source_ids.append(analisis.source_id)
        for tema in analisis.temas:
            valor = str(tema).strip()
            if valor and valor not in temas:
                temas.append(valor)
        if analisis.categoria_enrutamiento not in categorias:
            categorias.append(analisis.categoria_enrutamiento)
        if analisis.sentimiento not in sentimientos:
            sentimientos.append(analisis.sentimiento)
        if analisis.apto_para_publicacion:
            texto_limpio = _enmascarar_pii(interaccion.texto.strip())
            if texto_limpio:
                textos_publicables.append(texto_limpio)
        else:
            casos_no_publicables.append(registrar_no_publicable(analisis))

    if not source_ids:
        raise ValueError("No hay análisis vinculados a interacciones para generar el resumen semanal.")

    resumen_base = (
        f"Temas destacados: {', '.join(temas) or 'sin temas detallados'}. "
        f"Categorías observadas: {', '.join(categorias) or 'sin categorías'}. "
        f"Sentimientos observados: {', '.join(sentimientos) or 'sin clasificación'}."
    )
    if textos_publicables:
        resumen_base += " Actividad publicable: " + " | ".join(textos_publicables)
    resumen = ResumenSemanal(
        seccion="Temas destacados",
        titular=f"Resumen semanal: {', '.join(temas[:2]) or 'actividad comunitaria'}"[:200],
        resumen=_texto_minimo(resumen_base),
        source_ids=list(dict.fromkeys(source_ids)),
    )
    return resumen, casos_no_publicables


def generar_activos(
    interacciones: list[InteraccionValidada], analisis_lista: list[MensajeAnalizado]
) -> ActivosGenerados:
    """Generador determinista original: produce ambos formatos y casos para curaduría."""
    resumen_semanal, casos_no_publicables = generar_resumen_semanal(interacciones, analisis_lista)
    por_id = {interaccion.id: interaccion for interaccion in interacciones}
    candidatos = [
        mensaje
        for mensaje in analisis_lista
        if mensaje.apto_para_publicacion and mensaje.source_id in por_id
    ]
    if not candidatos:
        raise ValueError("No se encontró ninguna interacción apta para generar PostLinkedIn")
    mejor = max(candidatos, key=lambda mensaje: mensaje.puntuacion_relevancia.total)
    post_linkedin = generar_post_linkedin(por_id[mejor.source_id], mejor)
    if post_linkedin is None:
        raise ValueError("No se pudo construir un PostLinkedIn válido a partir del análisis")

    resultado = ActivosGenerados(
        post_linkedin=post_linkedin,
        resumen_semanal=resumen_semanal,
        casos_no_publicables=casos_no_publicables,
    )
    ids_validos = {mensaje.source_id for mensaje in analisis_lista}
    ids_activos = (
        resultado.post_linkedin.source_ids
        + resultado.resumen_semanal.source_ids
        + [caso.source_id for caso in resultado.casos_no_publicables]
    )
    if not set(ids_activos).issubset(ids_validos):
        raise ValueError("Hay activos con source_id sin respaldo en el análisis")
    return resultado


def construir_paquete_salida(
    analisis_cognitivo: AnalisisCognitivo,
    activos: ActivosGenerados,
    metadatos_ejecucion: MetadatosEjecucion,
) -> PaqueteSalida:
    """Construye el contrato de salida y valida trazabilidad de todos los activos."""
    source_ids_validos = {mensaje.source_id for mensaje in analisis_cognitivo.mensajes_analizados}
    source_ids_activos = (
        activos.post_linkedin.source_ids
        + activos.resumen_semanal.source_ids
        + [caso.source_id for caso in activos.casos_no_publicables]
    )
    if not set(source_ids_activos).issubset(source_ids_validos):
        raise ValueError("Hay activos con source_id sin respaldo en el análisis")
    return PaqueteSalida(
        run_id=analisis_cognitivo.run_id,
        request_id=analisis_cognitivo.request_id,
        metadatos_ejecucion=metadatos_ejecucion,
        analisis_resumido=analisis_cognitivo.resumen_comunidad,
        activos=activos,
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            ruta_objeto=(
                f"activos/periodo-no-asignado/{analisis_cognitivo.run_id}/revision-001.json"
            ),
            status_almacenamiento="no_iniciado",
            comprobacion_lectura=False,
        ),
    )


def _activos_degradados(
    interaccion_post: InteraccionValidada,
    mensaje_post: MensajeAnalizado,
    registros: list[dict[str, Any]],
    ids_resumen: list[str],
) -> tuple[PostLinkedIn, ResumenSemanal]:
    """Plantillas seguras: solo usan hechos disponibles, sanitizan PII y no reproducen quejas privadas."""
    temas_post = [str(tema) for tema in mensaje_post.temas if str(tema).strip()]
    tema_post = temas_post[0] if temas_post else "comunidad tecnológica"
    titulo_post = f"Comunidad: {tema_post}"[:200]
    texto_post_seguro = _enmascarar_pii(interaccion_post.texto.strip())
    cuerpo_post = _texto_minimo(
        f"{texto_post_seguro} Contexto documentado: {', '.join(temas_post) or tema_post}. "
        f"Canal de origen: {interaccion_post.canal}."
    )
    post = PostLinkedIn(
        titulo=titulo_post,
        cuerpo=cuerpo_post,
        hashtags=[
            f"#{''.join(c for c in tema.title() if c.isalnum())}" for tema in temas_post[:5]
        ],
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
        "Sentimientos observados: "
        + (", ".join(sentimientos) or "sin clasificación disponible")
        + "."
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
    timeout_seg: float | None = None,
) -> tuple[PostLinkedIn, ResumenSemanal, TelemetriaGeneracion]:
    inicio = time.perf_counter()
    proveedor = _proveedor_activo()
    modelo = resolver_modelo(proveedor)
    timeout_efectivo = timeout_seg or _leer_flotante_env(
        "LLM_INFERENCE_TIMEOUT_SEC", TIMEOUT_INFERENCIA_DEFECTO_SEG
    )

    interaccion_post, mensaje_post = _seleccionar_fuente(
        analisis_cognitivo, interacciones_validas, enfoque_editorial
    )
    registros, ids_resumen = _registros_resumen(interacciones_validas, analisis_cognitivo)
    texto_origen_post = _enmascarar_pii(interaccion_post.texto)

    # Definir ambos prompts ANTES de calcular su huella; evita UnboundLocalError.
    system_post = (
        "Eres redactor de una comunidad tecnológica. Redacta una propuesta profesional "
        "para LinkedIn usando exclusivamente los datos proporcionados. No inventes cifras, "
        "logros ni resultados. No afirmes que está aprobada o publicada. "
        "Prohibido agregar reflexiones personales, proyecciones de carrera o emociones en primera "
        "persona que no figuren literalmente en el texto de origen"
    )
    system_resumen = (
        "Eres editor de una comunidad tecnológica. Resume la actividad semanal para revisión "
        "humana, sin inventar hechos ni reproducir textos individuales de casos no publicables."
    )
    huella = hashlib.sha256(f"{system_post}\n---\n{system_resumen}".encode("utf-8")).hexdigest()[
        :12
    ]
    version = f"{VERSION_PROMPTS_GENERACION}+sha256:{huella}"

    prompt_post = (
        f"run_id={run_id}; request_id={request_id}; enfoque={enfoque_editorial}\n"
        f"source_id autorizado={mensaje_post.source_id}\n"
        f"categoría={mensaje_post.categoria_enrutamiento}\n"
        f"temas={mensaje_post.temas}; entidades={mensaje_post.entidades_relevantes}\n"
        f"texto de origen={texto_origen_post}\n"
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
        agent_resumen = CognitiveAgentFactory.create_agent(
            proveedor, system_resumen, ResumenSemanal
        )

        async def ejecutar(agent: Any, prompt: str):
            settings = _ajustes(agent, temperatura_cero)
            coro = (
                agent.run(prompt)
                if settings is None
                else agent.run(prompt, model_settings=settings)
            )
            return await asyncio.wait_for(coro, timeout=timeout_efectivo)

        resultado_post, resultado_resumen = await asyncio.gather(
            ejecutar(agent_post, prompt_post),
            ejecutar(agent_resumen, prompt_resumen),
        )
        post_generado = resultado_post.output
        resumen_generado = resultado_resumen.output
        post = PostLinkedIn.model_validate(
            {
                **post_generado.model_dump(),
                "cuerpo": _texto_minimo(post_generado.cuerpo),
                "source_ids": [mensaje_post.source_id],
            }
        )
        resumen = ResumenSemanal.model_validate(
            {
                **resumen_generado.model_dump(),
                "resumen": _texto_minimo(resumen_generado.resumen),
                "source_ids": ids_resumen,
            }
        )
        entrada_post, salida_post, cache_post = _tokens(resultado_post, proveedor)
        entrada_resumen, salida_resumen, cache_resumen = _tokens(resultado_resumen, proveedor)
        entrada = entrada_post + entrada_resumen
        salida = salida_post + salida_resumen
        cacheados = cache_post + cache_resumen
    except Exception as exc:  # noqa: BLE001 — incluye TimeoutError y fallos de red/esquema
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