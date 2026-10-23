"""Estado compartido del grafo de LangGraph (spec v3.1, sección 3).

Se implementa con `typing.TypedDict` y vive en un archivo desacoplado de
`pipeline_runner.py` para evitar importaciones circulares (sección 8, punto 1).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict

from ai_modules.cognitive_analyzer import TelemetriaAnalisis
from ai_modules.generador import TelemetriaGeneracion
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
    ruta_archivo_crudo: Optional[str]

    # 2. Datos en Tránsito y Auditoría
    interacciones_crudas: List[Dict[str, Any]]
    interacciones_validas: List[InteraccionValidada]
    registros_rechazados: List[Dict[str, Any]]  # filas de auditoria_rechazados

    # 3. Análisis Cognitivo (Contrato 2)
    analisis_cognitivo: Optional[AnalisisCognitivo]
    # Telemetría del análisis (modelo, prompt_version, latencia y tokens).
    # Modelo interno de ai_modules/cognitive_analyzer.py, no un contrato.
    telemetria_analisis: Optional[TelemetriaAnalisis]
    # Telemetría acumulada de la generación (incluye reintentos por invariantes).
    telemetria_generacion: Optional[TelemetriaGeneracion]

    # Enfoque editorial y el conteo que lo sustenta: se calculan dentro de
    # nodo_generar_copys (sección 4, Nodo 3). Son estado interno de
    # orquestación, no campos de un contrato canónico.
    enfoque_editorial: Optional[Literal["Logro", "Duda", "Dificultad"]]
    conteo_enfoque_editorial: Optional[Dict[str, int]]

    # 4. Activos Generados (Contrato 3)
    # `casos_no_publicables` viaja dentro de `activos_generados`, pero también
    # se conserva aparte para poder devolverlo en los caminos sin paquete.
    activos_generados: Optional[ActivosGenerados]
    casos_no_publicables: List[CasoNoPublicable]
    metadatos_ejecucion: Optional[MetadatosEjecucion]

    # 5. Persistencia y Evidencia
    paquete_salida: Optional[PaqueteSalida]
    almacenamiento_oci: Optional[EvidenciaAlmacenamientoOCI]

    # 6. Control de Errores y Diagnóstico
    errores: List[str]
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
