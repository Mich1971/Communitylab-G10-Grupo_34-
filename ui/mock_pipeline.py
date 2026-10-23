"""
Puente temporal entre la capa de validación (Andrea, ya real) y las capas que
todavía no existen en este repo: `ai_modules/cognitive_analyzer.py` (Michael),
`ai_modules/copy_generator.py` (Roberto) y `storage/oci_client.py` (Michael).

Nada aquí llama a un LLM real ni a OCI real: son heurísticas simples y una
simulación local en disco, solo para que el panel (`ui/app.py`) sea demostrable
de punta a punta mientras esas piezas se integran. Cuando esos módulos existan,
`analizar_mock` y `generar_copys_mock` se reemplazan por sus llamadas reales y
`guardar_paquete` delega automáticamente en `storage.oci_client` si lo detecta.
"""

import hashlib
import json
import uuid
from pathlib import Path
from typing import List

from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MensajeAnalizado,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)

OCI_SIM_DIR = Path("data/oci_sim")

PALABRAS_LOGRO = ("logré", "logre", "conseguí", "consegui", "terminé", "termine", "completamos", "contratad")
PALABRAS_DIFICULTAD = ("problema", "no funciona", "error", "bloqueo", "no puedo")


def _clasificar(texto: str, tipo: str) -> tuple[str, str, str]:
    """Heurística simple de clasificación. Devuelve (sentimiento, categoria_enrutamiento, regla_aplicada)."""
    texto_low = texto.lower()
    if any(p in texto_low for p in PALABRAS_LOGRO) or tipo.lower() == "testimonio":
        return "Altamente Positivo", "Logro", "Contiene lenguaje de logro o está marcado como testimonio"
    if "?" in texto or tipo.lower() == "duda":
        return "Neutro", "Duda", "Contiene una pregunta o está marcado como duda"
    if any(p in texto_low for p in PALABRAS_DIFICULTAD):
        return "Dificultad/Negativo", "Dificultad", "Contiene lenguaje asociado a un bloqueo o problema"
    return "Positivo", "Duda", "Sin señales claras de logro o dificultad; se enruta como duda por defecto"


_PUNTAJES_POR_CATEGORIA = {
    "Logro": PuntuacionRelevancia(evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2, total=6),
    "Duda": PuntuacionRelevancia(evidencia_explicita=1, utilidad_comunitaria=2, claridad_contexto=1, total=4),
    "Dificultad": PuntuacionRelevancia(evidencia_explicita=1, utilidad_comunitaria=1, claridad_contexto=1, total=3),
}


def analizar_mock(validos: List[InteraccionValidada], run_id: str, request_id: str) -> AnalisisCognitivo:
    mensajes: List[MensajeAnalizado] = []
    distribucion: dict[str, int] = {}

    for item in validos:
        sentimiento, categoria, regla = _clasificar(item.texto, item.tipo)
        puntuacion = _PUNTAJES_POR_CATEGORIA[categoria]
        mensajes.append(
            MensajeAnalizado(
                source_id=item.id,
                sentimiento=sentimiento,
                categoria_enrutamiento=categoria,
                regla_aplicada=regla,
                temas=[item.canal, item.tipo],
                entidades_relevantes=[],
                puntuacion_relevancia=puntuacion,
                motivo_seleccion=f"Clasificado como '{categoria}' por regla heurística: {regla.lower()}.",
                requiere_soporte=(categoria == "Dificultad"),
                apto_para_publicacion=(categoria == "Logro"),
            )
        )
        distribucion[sentimiento] = distribucion.get(sentimiento, 0) + 1

    if len(distribucion) == 1:
        predominante = next(iter(distribucion))
    else:
        max_conteo = max(distribucion.values())
        empatados = [s for s, c in distribucion.items() if c == max_conteo]
        predominante = empatados[0] if len(empatados) == 1 else "Mixto"

    return AnalisisCognitivo(
        run_id=run_id,
        request_id=request_id,
        resumen_comunidad=ResumenComunidad(
            total_interacciones_procesadas=len(validos),
            registros_validos=len(validos),
            registros_rechazados=0,
            sentimiento_predominante=predominante,
            distribucion_sentimiento=distribucion,
            temas_principales=sorted({tema for m in mensajes for tema in m.temas}) or ["Sin temas detectados"],
            alertas_soporte=[m.source_id for m in mensajes if m.requiere_soporte],
        ),
        mensajes_analizados=mensajes,
    )


def generar_copys_mock(analisis: AnalisisCognitivo) -> ActivosGenerados | None:
    """Devuelve None si no hay información válida suficiente para generar contenido con sustento."""
    logros = [m for m in analisis.mensajes_analizados if m.apto_para_publicacion]
    if not logros:
        return None

    principal = max(logros, key=lambda m: m.puntuacion_relevancia.total)
    todos_los_ids = [m.source_id for m in analisis.mensajes_analizados]

    post = PostLinkedIn(
        titulo="Logros de nuestra comunidad esta semana",
        cuerpo=(
            f"Celebramos un nuevo hito dentro de la comunidad, respaldado por el mensaje {principal.source_id}. "
            "Este tipo de logros muestra el impacto real del aprendizaje colaborativo."
        ),
        hashtags=["#ComunidadTech", "#Aprendizaje"],
        source_ids=[principal.source_id],
    )
    resumen = ResumenSemanal(
        seccion="Resumen semanal",
        titular="Logros y dudas de la comunidad esta semana",
        resumen=(
            f"Esta semana se registraron {len(analisis.mensajes_analizados)} interacciones válidas, "
            f"con sentimiento predominante '{analisis.resumen_comunidad.sentimiento_predominante}'."
        ),
        source_ids=todos_los_ids,
    )
    return ActivosGenerados(post_linkedin=post, resumen_semanal=resumen)


def guardar_paquete(paquete_dict: dict, ruta_objeto: str) -> EvidenciaAlmacenamientoOCI:
    """
    Intenta delegar en storage.oci_client (real) si ya existe en el repo.
    Si no existe todavía, simula guardado+lectura en disco local, marcando
    con claridad que es una simulación, nunca un falso éxito de OCI real.
    """
    try:
        from storage.oci_client import subir_y_verificar_paquete  # type: ignore

        return subir_y_verificar_paquete(paquete_dict, ruta_objeto)
    except ImportError:
        pass

    OCI_SIM_DIR.mkdir(parents=True, exist_ok=True)
    destino = OCI_SIM_DIR / ruta_objeto.replace("/", "__")
    contenido = json.dumps(paquete_dict, ensure_ascii=False, indent=2, default=str)

    destino.write_text(contenido, encoding="utf-8")
    releido = destino.read_text(encoding="utf-8")
    coincide = hashlib.sha256(contenido.encode("utf-8")).hexdigest() == hashlib.sha256(releido.encode("utf-8")).hexdigest()

    return EvidenciaAlmacenamientoOCI(
        bucket="communitylab-activos-marketing (SIMULADO local)",
        ruta_objeto=ruta_objeto,
        status_almacenamiento="guardado_con_exito" if coincide else "guardado_error",
        comprobacion_lectura=coincide,
    )


def generar_run_id(periodo_referencia: str) -> str:
    return f"run-{periodo_referencia}-{uuid.uuid4().hex[:8]}"
