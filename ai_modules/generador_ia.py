"""Adaptador legado de Gemini, conservado para compatibilidad con el Contrato 3.

El flujo oficial del orquestador usa ``generador.generar_activos_marketing``.
Este módulo mantiene las funciones síncronas anteriores, pero delega las reglas
comunes al paquete ai_modules, sanitiza PII y evita duplicados, imports planos y texto privado.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from pydantic import ValidationError
import structlog

from ai_modules.cognitive_analyzer import _enmascarar_pii
from ai_modules.generador import (
    _texto_minimo,
    buscar_interaccion_por_source_id,
    construir_paquete_salida,
    registrar_no_publicable,
)
from ai_modules.multiprovider_agent import resolver_modelo
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    ResumenSemanal,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
VERSION_PROMPTS = "1.0.1"
logger = structlog.get_logger("ai_modules.generador_ia")

# Carga configuración del proyecto una sola vez sin sobreescribir variables
# que ya haya definido el entorno de ejecución.
load_dotenv(dotenv_path=ENV_PATH, override=False)
MODELO_GEMINI = resolver_modelo("gemini")


def crear_cliente_gemini():
    """Carga la configuración desde la raíz y construye el cliente Gemini."""
    load_dotenv(dotenv_path=ENV_PATH, override=False)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "tu_api_key_aqui":
        raise ValueError("GEMINI_API_KEY no está configurada.")
    return genai.Client(api_key=api_key)


def _uso_tokens(respuesta: Any) -> tuple[int, int]:
    """Extrae tokens reales de usage_metadata; tolera SDKs/respuestas sin métricas."""
    usage = getattr(respuesta, "usage_metadata", None)
    if usage is None:
        usage = getattr(respuesta, "usage", None)
    if usage is None:
        return 0, 0

    def leer(*nombres: str) -> int:
        for nombre in nombres:
            if isinstance(usage, dict) and nombre in usage:
                valor = usage[nombre]
            else:
                valor = getattr(usage, nombre, None)
            if valor is not None:
                try:
                    return max(0, int(valor))
                except (TypeError, ValueError):
                    return 0
        return 0

    entrada = leer("prompt_token_count", "input_tokens")
    salida = leer("candidates_token_count", "output_tokens")
    return entrada, salida


def _solicitar_modelo(cliente: Any, prompt: str, esquema: type):
    modelo = resolver_modelo("gemini")
    respuesta = cliente.models.generate_content(
        model=modelo,
        contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": esquema},
    )
    if not getattr(respuesta, "text", None):
        raise ValueError("Gemini devolvió una respuesta vacía.")
    try:
        objeto = json.loads(respuesta.text)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Gemini devolvió JSON inválido para {esquema.__name__}.") from error
    if not isinstance(objeto, dict):
        raise ValueError(f"Gemini no devolvió un objeto JSON para {esquema.__name__}.")
    entrada, salida = _uso_tokens(respuesta)
    return objeto, entrada, salida


def _generar_post_detalle(
    cliente: Any, interaccion: InteraccionValidada, analisis: MensajeAnalizado
) -> tuple[PostLinkedIn, int, int]:
    if interaccion.id != analisis.source_id:
        raise ValueError("La interacción y el análisis no corresponden al mismo source_id")
    if not analisis.apto_para_publicacion:
        raise ValueError("No se puede generar una publicación con un análisis no publicable")

    texto_seguro = _enmascarar_pii(interaccion.texto)
    prompt = f"""
Eres redactor de comunicación para una comunidad tecnológica.
Genera una propuesta para LinkedIn usando exclusivamente los datos dados.
No inventes logros, cifras, personas ni resultados; conserva el sentido del mensaje.
Usa tono profesional y cercano. El cuerpo debe tener al menos 20 caracteres.
Genera hashtags relacionados con los temas. No afirmes que fue aprobada o publicada.
Devuelve exclusivamente el contrato PostLinkedIn en JSON.

source_id autorizado: {analisis.source_id}
Mensaje publicable original: {texto_seguro}
Temas identificados: {analisis.temas}
Entidades relevantes: {analisis.entidades_relevantes}
"""
    datos_post, entrada, salida = _solicitar_modelo(cliente, prompt, PostLinkedIn)
    cuerpo = str(datos_post.get("cuerpo") or "").strip()
    if not cuerpo:
        raise ValueError("Gemini devolvió una publicación sin cuerpo.")
    datos_post["cuerpo"] = _texto_minimo(cuerpo)
    # Los IDs válidos se fuerzan antes de validar; nunca dependen del LLM.
    datos_post["source_ids"] = [analisis.source_id]
    try:
        post = PostLinkedIn.model_validate(datos_post)
    except ValidationError as error:
        raise ValueError("Gemini devolvió una publicación con estructura inválida.") from error
    return post, entrada, salida


def generar_post_con_gemini(interaccion, analisis):
    """Genera una publicación validada; source_ids procede del análisis, no del LLM."""
    cliente = crear_cliente_gemini()
    post, _, _ = _generar_post_detalle(cliente, interaccion, analisis)
    return post


def _registros_resumen(interacciones, analisis_lista):
    por_id = {interaccion.id: interaccion for interaccion in interacciones}
    registros: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for analisis in analisis_lista:
        interaccion = por_id.get(analisis.source_id)
        if interaccion is None or not analisis.temas:
            continue
        registro = {
            "source_id": analisis.source_id,
            "sentimiento": analisis.sentimiento,
            "categoria": analisis.categoria_enrutamiento,
            "temas": analisis.temas,
            "apto_para_publicacion": analisis.apto_para_publicacion,
        }
        # El texto original solo puede entrar al prompt si el mensaje es publicable (y sanitizado).
        if analisis.apto_para_publicacion:
            registro["texto"] = _enmascarar_pii(interaccion.texto)
        registros.append(registro)
        source_ids.append(analisis.source_id)
    if not registros:
        raise ValueError(
            "No hay análisis vinculados a interacciones para generar el resumen semanal."
        )
    return registros, list(dict.fromkeys(source_ids))


def _generar_resumen_detalle(cliente: Any, interacciones, analisis_lista):
    registros, source_ids = _registros_resumen(interacciones, analisis_lista)
    prompt = f"""
Eres responsable de resumir la actividad semanal de una comunidad tecnológica.
Redacta un resumen profesional y útil para revisión humana. Usa únicamente los datos.
No inventes logros, cifras ni resultados. No reproduzcas texto individual de casos no publicables;
puedes sintetizar sus temas de forma general. No afirmes que el resumen está aprobado o publicado.
Devuelve exclusivamente el contrato ResumenSemanal en JSON.

Datos de la semana: {registros}
"""
    datos_resumen, entrada, salida = _solicitar_modelo(cliente, prompt, ResumenSemanal)
    texto_resumen = str(datos_resumen.get("resumen") or "").strip()
    if not texto_resumen:
        raise ValueError("Gemini devolvió un resumen sin contenido.")
    datos_resumen["resumen"] = _texto_minimo(texto_resumen)
    datos_resumen["source_ids"] = source_ids
    try:
        resumen = ResumenSemanal.model_validate(datos_resumen)
    except ValidationError as error:
        raise ValueError("Gemini devolvió un resumen con estructura inválida.") from error
    return resumen, entrada, salida


def generar_resumen_con_gemini(interacciones, analisis_lista):
    """Genera resumen sin exponer texto de registros no publicables."""
    cliente = crear_cliente_gemini()
    resumen, _, _ = _generar_resumen_detalle(cliente, interacciones, analisis_lista)
    return resumen


def _generar_activos_con_metricas(
    interacciones: list[InteraccionValidada], analisis_lista: list[MensajeAnalizado]
) -> tuple[ActivosGenerados, int, int]:
    ids_interacciones = {interaccion.id for interaccion in interacciones}
    por_id = {interaccion.id: interaccion for interaccion in interacciones}
    candidatos = [
        mensaje
        for mensaje in analisis_lista
        if mensaje.apto_para_publicacion and mensaje.source_id in por_id
    ]
    if not candidatos:
        raise ValueError("No se encontró ninguna interacción apta para generar PostLinkedIn con IA.")
    mejor = max(candidatos, key=lambda mensaje: mensaje.puntuacion_relevancia.total)

    cliente = crear_cliente_gemini()
    post, entrada_post, salida_post = _generar_post_detalle(
        cliente, por_id[mejor.source_id], mejor
    )
    resumen, entrada_resumen, salida_resumen = _generar_resumen_detalle(
        cliente, interacciones, analisis_lista
    )
    casos = [
        registrar_no_publicable(analisis)
        for analisis in analisis_lista
        if not analisis.apto_para_publicacion and analisis.source_id in ids_interacciones
    ]
    activos = ActivosGenerados(
        post_linkedin=post,
        resumen_semanal=resumen,
        casos_no_publicables=casos,
    )
    ids_validos = {mensaje.source_id for mensaje in analisis_lista}
    ids_activos = post.source_ids + resumen.source_ids + [caso.source_id for caso in casos]
    if not set(ids_activos).issubset(ids_validos):
        raise ValueError("Hay activos con source_id sin respaldo en el análisis.")
    return activos, entrada_post + entrada_resumen, salida_post + salida_resumen


def generar_activos_con_gemini(interacciones, analisis_lista):
    """API de compatibilidad: genera PostLinkedIn, ResumenSemanal y casos curables."""
    activos, _, _ = _generar_activos_con_metricas(interacciones, analisis_lista)
    return activos


def generar_paquete_con_gemini(
    analisis_cognitivo: AnalisisCognitivo, interacciones: list[InteraccionValidada]
) -> PaqueteSalida:
    """Construye el paquete legado con latencia y conteo real de tokens."""
    inicio = time.perf_counter()
    activos, tokens_entrada, tokens_salida = _generar_activos_con_metricas(
        interacciones, analisis_cognitivo.mensajes_analizados
    )
    metadatos = MetadatosEjecucion(
        modelo=resolver_modelo("gemini"),
        prompt_version=VERSION_PROMPTS,
        latencia_ms=round((time.perf_counter() - inicio) * 1000),
        tokens_totales=tokens_entrada + tokens_salida,
    )
    logger.info(
        "generacion_activos_completada",
        run_id=analisis_cognitivo.run_id,
        request_id=analisis_cognitivo.request_id,
        proveedor="gemini",
        modelo=metadatos.modelo,
        prompt_version=metadatos.prompt_version,
        latencia_ms=metadatos.latencia_ms,
        tokens_entrada=tokens_entrada,
        tokens_salida=tokens_salida,
        tokens_totales=metadatos.tokens_totales,
        post_source_ids=activos.post_linkedin.source_ids,
        resumen_source_ids=activos.resumen_semanal.source_ids,
    )
    return construir_paquete_salida(analisis_cognitivo, activos, metadatos)