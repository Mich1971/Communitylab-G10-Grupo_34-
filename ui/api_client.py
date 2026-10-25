"""Transporte del panel. El secreto nunca se envía al navegador ni a los logs."""

from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from storage import modelos_curaduria as models


def validate_response(method, path, data):
    """Validar la frontera HTTP reutilizando los modelos oficiales, sin SQL."""
    parts = path.strip("/").split("/")
    model = None
    if path == "/runs":
        model = models.RespuestaListarEjecuciones
    elif len(parts) >= 3 and parts[0] == "runs":
        model = {
            "detalle": models.RespuestaDetalleEjecucion,
            "mensajes": models.RespuestaListarMensajes,
            "fuentes": models.RespuestaObtenerFuentes,
            "resumen": models.RespuestaObtenerResumen,
            "rechazados": models.RespuestaListarRechazados,
            "revisiones": models.RespuestaListarRevisiones,
        }.get(parts[2])
        if parts[2] == "revisiones":
            if len(parts) == 5:
                model = models.RespuestaGuardarOCI
            elif len(parts) == 4:
                model = models.RespuestaObtenerRevision
            elif method == "POST":
                model = models.RespuestaRegistrarRevision
    if model:
        return model.model_validate(data).model_dump(mode="json")
    if not isinstance(data.get("run_id"), str) or not isinstance(
        data.get("estado"), str
    ):
        raise TypeError("Respuesta de admisión/estado incompleta")
    return data


class UISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )
    api_url: str = Field(
        default="http://127.0.0.1:8000", validation_alias="COMMUNITYLAB_API_URL"
    )
    webhook_secret: SecretStr = Field(
        validation_alias="COMMUNITYLAB_WEBHOOK_SECRET", min_length=16
    )

    @field_validator("api_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("URL de API inválida")
        return value.rstrip("/")


class APIError(Exception):
    def __init__(self, message, *, status=None, revision_actual=None, uncertain=False):
        super().__init__(message)
        self.status = status
        self.revision_actual = revision_actual
        self.uncertain = uncertain


class APIClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def get(self, path, **params):
        return self.request(
            "GET", path, params={k: v for k, v in params.items() if v is not None}
        )

    def post(self, path, payload=None):
        return self.request("POST", path, json=payload)

    def request(self, method, path, **kwargs):
        # No se reintentan mutaciones: una respuesta perdida no prueba que
        # la operación haya fallado. El curador debe consultar el histórico.
        secret = self.settings.webhook_secret.get_secret_value()
        try:
            with httpx.Client(
                transport=self.transport,
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
            ) as client:
                response = client.request(
                    method,
                    self.settings.api_url + "/api/v1" + path,
                    headers={"X-Webhook-Secret": secret},
                    **kwargs,
                )
        except httpx.RequestError:
            raise APIError(
                "No se pudo confirmar la respuesta del backend. Conservamos tu trabajo; consulta el estado antes de repetir.",
                uncertain=method == "POST",
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise APIError(
                "El backend devolvió una respuesta no válida.",
                status=response.status_code,
                uncertain=method == "POST",
            ) from None
        if not isinstance(data, dict):
            raise APIError(
                "El backend devolvió una respuesta no válida.",
                uncertain=method == "POST",
            )
        if not 200 <= response.status_code < 300:
            messages = {
                401: "La API rechazó la autenticación. Revisa la configuración del servidor.",
                404: "La ejecución o revisión solicitada no existe.",
                409: "Hay un conflicto con los datos guardados. Tu edición se conserva.",
                413: "El archivo supera el tamaño permitido por la API.",
                422: "Los datos no cumplen el contrato. Revisa el archivo o los campos editados.",
                503: "El backend no está disponible temporalmente. Intenta consultar nuevamente.",
            }
            # Mensajes propios: las respuestas técnicas pueden contener entradas
            # privadas o credenciales; no se imprimen directamente en el panel.
            raise APIError(
                messages.get(
                    response.status_code, "La API no pudo completar la operación."
                ),
                status=response.status_code,
                revision_actual=data.get("revision_actual"),
                uncertain=method == "POST" and response.status_code >= 500,
            )
        try:
            return validate_response(method, path, data)
        except (ValidationError, ValueError, TypeError):
            raise APIError(
                "La respuesta del backend no cumple el contrato esperado.",
                uncertain=method == "POST",
            ) from None


def get_client():
    return APIClient(UISettings())
