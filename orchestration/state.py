"""Estado compartido del grafo de LangGraph (spec v3.1, sección 3).

Se implementa con `typing.TypedDict` y vive en un archivo desacoplado de
`pipeline_runner.py` para evitar importaciones circulares (sección 8, punto 1).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from ai_modules.cognitive_analyzer import TelemetriaAnalisis
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MetadatosEjecucion,
    PaqueteSalida,
)


class PipelineState(TypedDict):
    """Estado global transferido entre nodos del grafo de LangGraph."""

    # 1. Identificadores Operativos e Idempotencia
    run_id: str
    request_id: str
    origen_comunidad: str
    periodo_referencia: str
    ruta_archivo_crudo: str | None

    # 2. Datos en Tránsito y Auditoría
    interacciones_crudas: list[dict[str, Any]]
    interacciones_validas: list[InteraccionValidada]
    registros_rechazados: list[dict[str, Any]]  # filas de auditoria_rechazados

    # 3. Análisis Cognitivo (Contrato 2)
    analisis_cognitivo: AnalisisCognitivo | None
    # Telemetría del análisis (modelo, prompt_version, latencia y tokens).
    # Modelo interno de ai_modules/cognitive_analyzer.py, no un contrato.
    telemetria_analisis: TelemetriaAnalisis | None

    # Enfoque editorial y el conteo que lo sustenta: se calculan dentro de
    # nodo_generar_copys (sección 4, Nodo 3). Son estado interno de
    # orquestación, no campos de un contrato canónico.
    enfoque_editorial: Literal["Logro", "Duda", "Dificultad"] | None
    conteo_enfoque_editorial: dict[str, int] | None

    # 4. Activos Generados (Contrato 3)
    # `casos_no_publicables` viaja dentro de `activos_generados`, pero también
    # se conserva aparte para poder devolverlo en los caminos sin paquete.
    activos_generados: ActivosGenerados | None
    casos_no_publicables: list[CasoNoPublicable]
    metadatos_ejecucion: MetadatosEjecucion | None

    # 5. Persistencia y Evidencia
    paquete_salida: PaqueteSalida | None
    almacenamiento_oci: EvidenciaAlmacenamientoOCI | None

    # 6. Control de Errores y Diagnóstico
    errores: list[str]
    estado_flujo: Literal[
        "iniciado",
        "validado",
        "analizado",
        "copys_generados",
        "completado",  # terminal: hay paquete
        "sin_registros_validos",  # terminal: sin paquete (lote vacío / 100 % rechazado)
        "sin_contenido_publicable",  # terminal: sin paquete (ningún mensaje apto)
        "fallido",  # terminal: sin paquete (error técnico)
    ]
