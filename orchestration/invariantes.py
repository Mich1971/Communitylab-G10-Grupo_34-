"""Invariantes puras del pipeline (enfoque editorial y trazabilidad factual).

Viven fuera de `pipeline_runner.py` porque este importa `ai_modules.generador`
y LangGraph: la API de curaduría necesita validar ediciones humanas con las
mismas reglas sin arrastrar esas dependencias. `pipeline_runner` las reexporta
para no romper a quienes ya las importan desde allí.
"""

from __future__ import annotations

from esquemas import AnalisisCognitivo, CasoNoPublicable, PostLinkedIn, ResumenSemanal

PRIORIDAD_EMPATE_ENFOQUE = ("Logro", "Dificultad", "Duda")  # a ratificar con el equipo editorial


def calcular_enfoque_editorial(analisis: AnalisisCognitivo) -> tuple[str, dict[str, int]]:
    """Enfoque editorial (M01): categoría mayoritaria entre los mensajes aptos.

    En empate gana `Logro` > `Dificultad` > `Duda`. Solo orienta el tono; nunca
    bifurca el formato de salida. Devuelve `(enfoque, conteo)`.
    """
    conteo: dict[str, int] = {"Logro": 0, "Duda": 0, "Dificultad": 0}
    for m in analisis.mensajes_analizados:
        if m.apto_para_publicacion:
            conteo[m.categoria_enrutamiento] += 1
    maximo = max(conteo.values())
    enfoque = next(c for c in PRIORIDAD_EMPATE_ENFOQUE if conteo[c] == maximo)
    return enfoque, conteo


def invariante_casos(analisis: AnalisisCognitivo, casos: list[CasoNoPublicable]) -> bool:
    """`casos_no_publicables` debe ser exactamente el conjunto de no aptos, sin repetidos."""
    no_aptos = {m.source_id for m in analisis.mensajes_analizados if not m.apto_para_publicacion}
    ids = [c.source_id for c in casos]
    return len(ids) == len(set(ids)) and set(ids) == no_aptos


def violaciones_trazabilidad(
    analisis: AnalisisCognitivo, post: PostLinkedIn, resumen: ResumenSemanal
) -> list[str]:
    """Invariantes 1 y 2 de la sección 6.3 (las del LLM). Lista vacía si se cumplen.

    * `PostLinkedIn` solo puede citar mensajes aptos (A).
    * `ResumenSemanal` puede citar cualquier mensaje procesado (M).
    * Ningún identificador puede ser vacío ni inexistente.
    """
    procesados = {m.source_id for m in analisis.mensajes_analizados}
    aptos = {m.source_id for m in analisis.mensajes_analizados if m.apto_para_publicacion}
    problemas: list[str] = []

    for nombre, activo, permitidos in (
        ("post_linkedin", post, aptos),
        ("resumen_semanal", resumen, procesados),
    ):
        ids = list(activo.source_ids)
        if not ids:
            problemas.append(f"{nombre}.source_ids está vacío")
        vacios = [s for s in ids if not s or not str(s).strip()]
        if vacios:
            problemas.append(f"{nombre}.source_ids contiene identificadores vacíos")
        fuera = sorted({s for s in ids if s and s not in permitidos})
        if fuera:
            regla = "fuera del conjunto de mensajes aptos" if nombre == "post_linkedin" else "inexistentes"
            problemas.append(f"{nombre}.source_ids {regla}: {fuera}")
    return problemas
