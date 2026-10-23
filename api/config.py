"""Configuración de la API perimetral (pydantic-settings).

Las variables se leen del entorno o de `.env`. El `.env` compartido contiene
también credenciales de OCI y de los LLM, por eso `extra="ignore"`.

El arranque falla de forma explícita si `COMMUNITYLAB_WEBHOOK_SECRET` falta
o es demasiado corto: una API sin secreto compartido no debe levantarse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    webhook_secret: str = Field(
        ...,
        min_length=16,
        validation_alias="COMMUNITYLAB_WEBHOOK_SECRET",
        repr=False,
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="LOG_LEVEL"
    )
    # Raíz de datos compartida por volumen con el runner y Streamlit.
    data_dir: Path = Field(default=Path("data"), validation_alias="COMMUNITYLAB_DATA_DIR")
    # Tope del cuerpo de la petición (un lote real pesa KB; 5 MiB sobra).
    max_body_bytes: int = Field(
        default=5 * 1024 * 1024, gt=0, validation_alias="COMMUNITYLAB_MAX_BODY_BYTES"
    )
    # Tiempo que una recepción "en curso" se considera viva antes de que un
    # reintento pueda retomarla (ver registro.py).
    gracia_recepcion_s: float = Field(
        default=30.0, gt=0, validation_alias="COMMUNITYLAB_GRACIA_RECEPCION_S"
    )
    # /docs y /openapi.json exponen el contrato; apagados por defecto porque
    # el puerto 8000 puede quedar expuesto (informe de infraestructura OCI).
    habilitar_docs: bool = Field(default=False, validation_alias="COMMUNITYLAB_ENABLE_DOCS")

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "communitylab.db"
