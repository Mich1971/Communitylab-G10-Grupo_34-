"""Modelos de resultado del orquestador (spec v3.1, sección 5.1).

`ResultadoPipeline` NO es un contrato canónico: es el valor de retorno del
orquestador. Vive en `orchestration/` y no en `esquemas.py`. Se mantiene en un
archivo propio, desacoplado de `pipeline_runner.py`, para evitar importaciones
circulares (sección 8, punto 1).
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from esquemas import CasoNoPublicable, PaqueteSalida

EstadoFlujoFinal = Literal[
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
]


class RegistroRechazado(BaseModel):
    """Espejo tipado de una fila de `auditoria_rechazados` (validador.py)."""

    mensaje_id: Optional[str] = None
    autor: Optional[str] = None
    canal: Optional[str] = None
    error_detalle: str


class ResultadoPipeline(BaseModel):
    estado_flujo: EstadoFlujoFinal
    run_id: str
    request_id: str
    registros_rechazados: List[RegistroRechazado] = Field(default_factory=list)
    casos_no_publicables: List[CasoNoPublicable] = Field(default_factory=list)
    errores: List[str] = Field(default_factory=list)
    paquete_salida: Optional[PaqueteSalida] = None

    @model_validator(mode="after")
    def paquete_solo_si_completado(self) -> "ResultadoPipeline":
        if (self.estado_flujo == "completado") != (self.paquete_salida is not None):
            raise ValueError(
                "paquete_salida debe estar presente si y solo si estado_flujo == 'completado'"
            )
        return self
