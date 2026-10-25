"""Modelos de respuesta de las consultas de curaduría (Fase 0 congelada).

Reutilizan los contratos de `esquemas.py` sin alterarlos. Viven en `storage/`
y no en `api/` para que la misma respuesta sirva a Python y a HTTP (paridad 1:1)
sin que la capa de almacenamiento dependa del transporte.

Diferencias deliberadas con el catálogo, todas aditivas o derivadas del plan:
`comunidad`, `periodo` y `fecha_recepcion` son opcionales porque un lote en cola
aún no los tiene y una ejecución manual no pasó por la API (sección 3 del plan);
`RespuestaDetalleEjecucion.advertencia` cubre el caso «completado sin paquete».
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from esquemas import (
    CategoriaEnrutamiento,
    EvidenciaAlmacenamientoOCI,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
    Sentimiento,
)

EstadoOperativo = Literal[
    "recibiendo",
    "en_cola",
    "procesando",
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
]
CategoriaEstado = Literal["en_curso", "finalizada_con_paquete", "finalizada_sin_paquete", "fallida"]
MotivoNoDisponible = Literal["en_curso", "sin_paquete", "sin_analisis"]


class SobreEstadoRun(BaseModel):
    """Encabezado común de toda consulta de sub-recurso de un `run_id`."""

    run_id: str
    request_id: str
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    disponible: bool
    motivo: MotivoNoDisponible | None = None


# 1. listar_ejecuciones
class ItemEjecucionResumen(BaseModel):
    run_id: str
    request_id: str
    fecha_recepcion: str | None = None
    fecha_procesamiento: str | None = None
    comunidad: str | None = None
    periodo: str | None = None
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    tiene_paquete: bool


class RespuestaListarEjecuciones(BaseModel):
    items: list[ItemEjecucionResumen]
    siguiente_cursor: str | None = None
    total: int = Field(..., ge=0)


# 2. obtener_ejecucion
class RespuestaDetalleEjecucion(BaseModel):
    run_id: str
    request_id: str
    comunidad: str | None = None
    periodo: str | None = None
    fecha_recepcion: str | None = None
    fecha_procesamiento: str | None = None
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    estado_flujo: str | None = None
    total_recibidos: int = Field(default=0, ge=0)
    total_validos: int = Field(default=0, ge=0)
    total_rechazados: int = Field(default=0, ge=0)
    tiene_paquete: bool
    revision_vigente: int | None = None
    advertencia: str | None = None


# 3. listar_mensajes
class ItemMensajeAnalizadoUI(BaseModel):
    source_id: str
    categoria_enrutamiento: CategoriaEnrutamiento
    sentimiento: Sentimiento
    puntuacion: PuntuacionRelevancia
    motivo_seleccion: str
    regla_aplicada: str
    temas: list[str]
    entidades: list[str]
    requiere_soporte: bool
    apto_para_publicacion: bool


class RespuestaListarMensajes(SobreEstadoRun):
    items: list[ItemMensajeAnalizadoUI] = Field(default_factory=list)
    siguiente_cursor: str | None = None
    total: int = Field(default=0, ge=0)


# 4. obtener_fuentes
class ItemFuenteOriginal(BaseModel):
    source_id: str
    autor: str
    canal: str
    tipo: str
    texto: str
    tipo_texto: Literal["original_validado"] = "original_validado"


class RespuestaObtenerFuentes(SobreEstadoRun):
    items: list[ItemFuenteOriginal] = Field(default_factory=list)
    no_encontrados: list[str] = Field(default_factory=list)


# 5. obtener_resumen
class RespuestaObtenerResumen(SobreEstadoRun):
    resumen: ResumenComunidad | None = None


# 6. listar_rechazados
class ItemRechazadoUI(BaseModel):
    mensaje_id: str | None = None
    autor: str | None = None
    canal: str | None = None
    motivo: str
    origen: Literal["validador", "analisis"]
    fecha_aislamiento: str
    texto_crudo: str | None = None


class RespuestaListarRechazados(SobreEstadoRun):
    items: list[ItemRechazadoUI] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)


# 7. listar_revisiones
class ItemRevisionResumen(BaseModel):
    numero_revision: int = Field(..., ge=1)
    estado: Literal["pendiente", "aprobado", "rechazado"]
    revisor: str | None = None
    comentarios: str | None = None
    fecha_decision: str | None = None
    fecha_generacion: str
    status_almacenamiento: Literal["guardado_con_exito", "guardado_error", "no_iniciado"]
    comprobacion_lectura: bool


class RespuestaListarRevisiones(SobreEstadoRun):
    revision_vigente: int | None = None
    items: list[ItemRevisionResumen] = Field(default_factory=list)


# 8. obtener_revision
class RespuestaObtenerRevision(BaseModel):
    run_id: str
    numero_revision: int
    paquete: PaqueteSalida
    evidencia_oci: EvidenciaAlmacenamientoOCI


# 9. registrar_revision
class EdicionRevisionInput(BaseModel):
    """Decisión del curador; los activos omitidos se copian de la revisión base."""

    estado: Literal["pendiente", "aprobado", "rechazado"]
    revisor: str | None = None
    comentarios: str | None = None
    post_linkedin: PostLinkedIn | None = None
    resumen_semanal: ResumenSemanal | None = None


class SolicitudRegistrarRevision(BaseModel):
    revision_base: int = Field(..., ge=1, description="Última revisión que el curador tiene en pantalla")
    edicion: EdicionRevisionInput


class RespuestaRegistrarRevision(BaseModel):
    run_id: str
    numero_revision: int
    estado: Literal["pendiente", "aprobado", "rechazado"]
    ruta_objeto: str


# 10. guardar_revision_en_oci
class RespuestaGuardarOCI(BaseModel):
    run_id: str
    numero_revision: int
    bucket: str
    ruta_objeto: str
    status_almacenamiento: Literal["guardado_con_exito", "guardado_error"]
    comprobacion_lectura: bool
    mensaje_error: str | None = None
    reintento_idempotente: bool = False
