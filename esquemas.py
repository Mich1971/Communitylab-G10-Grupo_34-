from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


# =====================================================================
# Contrato 1: Ingesta (n8n / Webhook / carga manual -> API)
# Validación en dos niveles: el sobre (IngestionLote) es permisivo para
# no tumbar el lote completo por un registro malo; la validación estricta
# de cada mensaje ocurre después, en InteraccionValidada.
# =====================================================================


class MetadataOrigen(BaseModel):
    """Metadatos de origen para la capa de admisión (permisiva, fecha como string)."""
    plataforma: str = Field(..., description="Plataforma de origen: Discord, Slack, Form, etc.")
    identificador_original: str = Field(..., description="ID nativo del mensaje en la plataforma")
    fecha: str = Field(..., description="Timestamp ISO-8601 del mensaje original")


class MetadataOrigenValidado(BaseModel):
    """Metadatos de origen validados para el pipeline interno (fecha parseada a datetime)."""
    plataforma: str = Field(..., description="Plataforma de origen: Discord, Slack, Form, etc.")
    identificador_original: str = Field(..., description="ID nativo del mensaje en la plataforma")
    fecha: datetime = Field(..., description="Timestamp parseado y validado como datetime")


class Interaccion(BaseModel):
    id: str
    autor: str = Field(default="Anónimo", description="Nombre o alias del miembro comunitario")
    canal: str
    tipo: str = Field(default="sin_clasificar")  # Si falta, asigna valor por defecto
    texto: Optional[str] = Field(default="", description="Puede venir vacío en pruebas de fallo")
    metadata_origen: MetadataOrigen


class IngestionLote(BaseModel):
    schema_version: str = Field(default="1.1.0")
    request_id: str = Field(..., description="Identificador de la solicitud provisto por el emisor (n8n)")
    origen_comunidad: str
    periodo_referencia: str
    interacciones: List[Interaccion] = Field(..., min_length=1)


class InteraccionValidada(BaseModel):
    """Segunda capa: aplicada por el proceso local antes de pasar el mensaje a los agentes de IA."""

    id: str
    autor: str
    canal: str
    tipo: str
    texto: str = Field(..., min_length=1, description="Texto no vacío exigido para análisis")
    metadata_origen: MetadataOrigenValidado

    @field_validator("texto")
    @classmethod
    def validar_texto_no_vacio(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El texto de la interacción no puede estar vacío ni contener solo espacios")
        return v.strip()


# =====================================================================
# Contrato 2: Análisis Cognitivo (salida del LLM sobre registros válidos)
# =====================================================================


# Alias compartidos por MensajeAnalizado (Contrato 2) y CasoNoPublicable
# (Contrato 3): una sola definición evita que los valores permitidos se
# desincronicen entre ambos contratos.
Sentimiento = Literal["Altamente Positivo", "Positivo", "Neutro", "Dificultad/Negativo"]
CategoriaEnrutamiento = Literal["Logro", "Duda", "Dificultad"]


class PuntuacionRelevancia(BaseModel):
    evidencia_explicita: int = Field(..., ge=0, le=2)
    utilidad_comunitaria: int = Field(..., ge=0, le=2)
    claridad_contexto: int = Field(..., ge=0, le=2)
    total: int = Field(default=0, ge=0, le=6)

    @model_validator(mode="after")
    def calcular_total(self):
        self.total = self.evidencia_explicita + self.utilidad_comunitaria + self.claridad_contexto
        return self


class MensajeAnalizado(BaseModel):
    source_id: str = Field(..., description="Referencia exacta al id del mensaje")
    sentimiento: Sentimiento
    categoria_enrutamiento: CategoriaEnrutamiento
    regla_aplicada: str = Field(..., description="Criterio documentado para derivar el contenido")
    temas: List[str] = Field(..., min_length=1)
    entidades_relevantes: List[str] = Field(default_factory=list)
    puntuacion_relevancia: PuntuacionRelevancia
    motivo_seleccion: str = Field(..., min_length=10, description="Justificación objetiva de la puntuación")
    requiere_soporte: bool = Field(default=False, description="Marca casos que requieren canalización o mentoría")
    apto_para_publicacion: bool = Field(..., description="Determina si el mensaje califica para difusión pública")


class ResumenComunidad(BaseModel):
    total_interacciones_procesadas: int = Field(..., ge=1)
    registros_validos: int = Field(..., ge=1)
    registros_rechazados: int = Field(default=0, ge=0)
    sentimiento_predominante: str = Field(..., description="Polaridad dominante o 'Mixto'")
    distribucion_sentimiento: Dict[str, int]
    temas_principales: List[str] = Field(..., min_length=1)
    alertas_soporte: List[str] = Field(default_factory=list)


class AnalisisCognitivo(BaseModel):
    run_id: str = Field(..., description="ID de ejecución asignado por la API")
    request_id: str = Field(..., description="ID de la solicitud original")
    resumen_comunidad: ResumenComunidad
    mensajes_analizados: List[MensajeAnalizado] = Field(..., min_length=1)


# =====================================================================
# Contrato 3: Distribución, curaduría y persistencia OCI
# =====================================================================


class PostLinkedIn(BaseModel):
    titulo: str = Field(..., min_length=5)
    canal_recomendado: str = Field(default="LinkedIn Oficial")
    cuerpo: str = Field(..., min_length=20)
    hashtags: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(..., min_length=1, description="IDs de mensajes que respaldan la publicación")


class ResumenSemanal(BaseModel):
    seccion: str
    titular: str = Field(..., min_length=5)
    resumen: str = Field(..., min_length=20)
    source_ids: List[str] = Field(..., min_length=1, description="IDs de mensajes que respaldan la síntesis")


class CasoNoPublicable(BaseModel):
    """Mensaje analizado que NO califica para difusión pública
    (`apto_para_publicacion=False`), expuesto al curador dentro del paquete.

    Es una vista derivada de `MensajeAnalizado`: debe construirse de forma
    determinista con `desde_mensaje_analizado`, nunca a partir de texto
    generado por un LLM.
    """

    source_id: str = Field(..., description="Referencia exacta al id del mensaje")
    sentimiento: Sentimiento
    categoria_enrutamiento: CategoriaEnrutamiento
    temas: List[str] = Field(..., min_length=1)
    requiere_soporte: bool = Field(default=False, description="Marca casos que requieren canalización o mentoría")
    motivo_seleccion: str = Field(..., min_length=10, description="Justificación objetiva de la puntuación")

    @classmethod
    def desde_mensaje_analizado(cls, mensaje: "MensajeAnalizado") -> "CasoNoPublicable":
        """Copia exacta de los campos homónimos; rechaza mensajes aptos."""
        if mensaje.apto_para_publicacion:
            raise ValueError(
                f"El mensaje {mensaje.source_id!r} es apto para publicación; no puede ser un caso no publicable"
            )
        return cls(
            source_id=mensaje.source_id,
            sentimiento=mensaje.sentimiento,
            categoria_enrutamiento=mensaje.categoria_enrutamiento,
            temas=list(mensaje.temas),
            requiere_soporte=mensaje.requiere_soporte,
            motivo_seleccion=mensaje.motivo_seleccion,
        )


class ActivosGenerados(BaseModel):
    post_linkedin: PostLinkedIn
    resumen_semanal: ResumenSemanal
    casos_no_publicables: List[CasoNoPublicable] = Field(
        default_factory=list,
        description="Mensajes con apto_para_publicacion=False, para revisión del curador (puede estar vacía)",
    )


class MetadatosEjecucion(BaseModel):
    modelo: str = Field(..., description="Identificador exacto del LLM")
    prompt_version: str = Field(..., description="Versión del prompt utilizado")
    latencia_ms: int = Field(..., ge=0)
    tokens_totales: int = Field(..., ge=0)


class EstadoRevision(BaseModel):
    numero_revision: int = Field(default=1, ge=1, description="Secuencia incremental de versión")
    estado: Literal["pendiente", "aprobado", "rechazado"] = "pendiente"
    revisor: Optional[str] = Field(default=None, description="Alias o identificador del curador humano")
    fecha_decision: Optional[datetime] = None
    comentarios: Optional[str] = None


class EvidenciaAlmacenamientoOCI(BaseModel):
    bucket: str = Field(default="communitylab-activos-marketing")
    ruta_objeto: str = Field(..., description="Ruta inmutable: activos/{periodo}/{run_id}/revision-{num}.json")
    status_almacenamiento: Literal["guardado_con_exito", "guardado_error", "no_iniciado"] = "no_iniciado"
    comprobacion_lectura: bool = Field(
        default=False, description="True solo si la aplicación recuperó y verificó el objeto en OCI"
    )


def fecha_utc_ahora() -> datetime:
    return datetime.now(timezone.utc)


class PaqueteSalida(BaseModel):
    schema_version: str = "1.1.0"
    run_id: str
    request_id: str
    fecha_generacion: datetime = Field(default_factory=fecha_utc_ahora)
    metadatos_ejecucion: MetadatosEjecucion
    analisis_resumido: ResumenComunidad
    activos: ActivosGenerados
    revision: EstadoRevision = Field(default_factory=EstadoRevision)
    almacenamiento_oci: EvidenciaAlmacenamientoOCI
