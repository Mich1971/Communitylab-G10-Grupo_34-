from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    IngestionLote,
    InteraccionValidada,
    PaqueteSalida,
)

EstadoFlujo = Literal[
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
]


class RegistroRechazado(BaseModel):
    id: str
    autor: str
    canal: str
    error: str
    texto_crudo: str | None = None
    metadata_origen: dict[str, Any] = Field(default_factory=dict)


class PipelineState(TypedDict, total=False):
    payload: IngestionLote
    run_id: str
    validos: list[InteraccionValidada]
    rechazados: list[RegistroRechazado]
    analisis: AnalisisCognitivo
    activos: ActivosGenerados
    paquete_salida: PaqueteSalida | None
    estado_flujo: EstadoFlujo
    motivo: str | None
    errores: list[str]
    mensajes_recibidos: int
    mensajes_validos: int
    mensajes_rechazados: int
    auditoria_sqlite_ok: bool


class ResultadoPipeline(BaseModel):
    """Resultado de orquestación; no sustituye al contrato PaqueteSalida."""

    estado_flujo: EstadoFlujo
    run_id: str
    request_id: str
    registros_rechazados: list[RegistroRechazado] = Field(default_factory=list)
    errores: list[str] = Field(default_factory=list)
    paquete_salida: PaqueteSalida | None
    motivo: str | None
    mensajes_recibidos: int
    mensajes_validos: int
    mensajes_rechazados: int
    auditoria_sqlite_ok: bool

    @model_validator(mode="after")
    def validar_paquete_segun_estado(self):
        if (self.estado_flujo == "completado") != (self.paquete_salida is not None):
            raise ValueError(
                "paquete_salida solo debe existir cuando estado_flujo es completado"
            )
        return self
