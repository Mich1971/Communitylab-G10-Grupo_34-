"""Cliente desacoplado para persistencia inmutable en OCI Object Storage (Always Free).

Versión corregida tras revisión técnica. Cambios respecto a la versión inicial:

1. Corrige el fallo de import: usa niveles de `logging`, no `structlog.INFO`
   (ese atributo no existe en structlog >= 22 y rompía `import storage.oci_client`
   con cualquier valor de LOG_LEVEL).
2. La configuración de `structlog` ya no se ejecuta al importar el módulo.
   `configure_logging()` se expone aparte para que la app la invoque UNA sola
   vez desde su punto de entrada (api/main.py, ui/app.py, etc.).
3. Inmutabilidad real: `put_object` usa `if_none_match="*"` y `content_md5`.
   Un 412 (la ruta ya existe) se resuelve comparando hashes: si coincide es
   un reintento idempotente válido; si no, es una colisión real y se reporta
   como error en vez de sobrescribir en silencio.
4. Las variables de entorno (.env) tienen prioridad sobre `~/.oci/config`
   cuando `OCI_USER_OCID` está definido, para que el perfil personal de un
   compañero no reemplace silenciosamente las credenciales de servicio.
5. `build_object_path` valida `periodo_referencia` y `run_id` contra una
   lista blanca (sin `/`, sin `..`) y exige `numero_revision >= 1`.
6. `upload_package` sólo acepta dict, str, bytes o un modelo Pydantic;
   cualquier otro tipo levanta un `TypeError` explícito en vez de subir
   `str(obj)` en silencio. Los `datetime`/`date` dentro de un dict se
   serializan a ISO-8601 en vez de fallar con un error no capturado.
   Si el payload ya trae `almacenamiento_oci.status_almacenamiento` distinto
   de "no_iniciado", se registra una advertencia: ese campo debe reflejar
   el resultado de ESTA subida, no un valor precompuesto por el llamador.
7. `retry_upload_from_disk` exige `request_id` explícito (antes se perdía y
   quedaba como "req-desconocido" en los logs) y valida que el archivo local
   sea JSON bien formado antes de subirlo — el hash SHA-256 solo demuestra
   integridad de transporte, no validez de contenido.
8. Se agregó `retry_strategy` a las llamadas de `put_object`/`get_object`, y
   un `timeout` de conexión/lectura al construir el cliente (el SDK no
   acepta `timeout` por llamada, solo en el constructor).
"""

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import oci
import structlog
from dotenv import load_dotenv
from oci.exceptions import ServiceError

# Cargar variables de entorno desde .env si existe. A diferencia de
# structlog.configure() (que sí puede pisar la configuración de quien
# importe este módulo), llamar load_dotenv() varias veces es inofensivo:
# no sobreescribe variables ya definidas en el entorno del sistema.
load_dotenv()

_RUTA_SEGMENTO_VALIDO = re.compile(r"^[A-Za-z0-9._-]+$")
_TIMEOUT_CONEXION_LECTURA = (10, 60)  # segundos: (connect, read) — valores por defecto del SDK, explícitos aquí


def configure_logging(level: int | None = None) -> None:
    """Configura structlog en JSON estructurado.

    Llamar UNA sola vez desde el punto de entrada de la aplicación — no
    ocurre al importar este módulo, para no pisar la configuración de
    logging de quien lo importe (FastAPI, Streamlit, LangGraph, etc.).
    """
    resolved_level = level
    if resolved_level is None:
        resolved_level = getattr(
            logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO
        )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger("storage.oci_client")


def _json_default(value: Any) -> str:
    """Solo permite serializar tipos con una representación inequívoca.
    Cualquier otro tipo debe fallar de forma explícita, nunca convertirse
    a texto en silencio."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(
        f"Objeto de tipo {type(value).__name__} no es serializable a JSON "
        "de forma segura; conviértalo explícitamente antes de subirlo."
    )


def _validar_segmento_ruta(valor: str, nombre_campo: str) -> str:
    valor = (valor or "").strip()
    if not valor or not _RUTA_SEGMENTO_VALIDO.match(valor):
        raise ValueError(
            f"{nombre_campo}={valor!r} contiene caracteres no permitidos. "
            "Solo se aceptan letras, números, '.', '_' y '-'."
        )
    return valor


@dataclass
class OCIConfig:
    """Parámetros de configuración validados para interactuar con OCI Object Storage Always Free."""

    user_ocid: str | None = None
    tenancy_ocid: str | None = None
    fingerprint: str | None = None
    key_file_path: str | None = None
    region: str | None = None
    namespace: str | None = None
    bucket_name: str = "communitylab-activos-marketing"
    config_file_path: str | None = None
    profile_name: str = "DEFAULT"

    @classmethod
    def from_env(cls) -> "OCIConfig":
        """Construye la configuración a partir de variables de entorno o rutas por defecto."""
        return cls(
            user_ocid=os.getenv("OCI_USER_OCID"),
            tenancy_ocid=os.getenv("OCI_TENANCY_OCID"),
            fingerprint=os.getenv("OCI_FINGERPRINT"),
            key_file_path=os.getenv("OCI_KEY_FILE"),
            region=os.getenv("OCI_REGION", "mx-queretaro-1"),
            namespace=os.getenv("OCI_NAMESPACE"),
            bucket_name=os.getenv(
                "OCI_BUCKET_NAME", "communitylab-activos-marketing"
            ),
            config_file_path=os.getenv("OCI_CONFIG_FILE"),
            profile_name=os.getenv("OCI_CONFIG_PROFILE", "DEFAULT"),
        )


@dataclass
class UploadResult:
    """Resultado detallado de la operación de persistencia y comprobación activa de lectura."""

    bucket: str
    ruta_objeto: str
    status_almacenamiento: str  # "guardado_con_exito" | "guardado_error"
    comprobacion_lectura: bool
    hash_sha256_subido: str | None = None
    hash_sha256_recuperado: str | None = None
    latencia_subida_ms: int = 0
    latencia_lectura_ms: int = 0
    mensaje_error: str | None = None
    reintento_idempotente: bool = False  # True si la ruta ya existía con el mismo contenido exacto


class OCIStorageClient:
    """Cliente desacoplado para persistencia inmutable en OCI Object Storage (Always Free)."""

    def __init__(self, config: OCIConfig | None = None) -> None:
        self.config = config or OCIConfig.from_env()
        self._client: oci.object_storage.ObjectStorageClient | None = None
        self._namespace: str | None = self.config.namespace
        self._log = logger.bind(
            component="OCIStorageClient", bucket=self.config.bucket_name
        )
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Inicializa el cliente. Las variables de entorno tienen prioridad
        sobre ~/.oci/config cuando OCI_USER_OCID está definido, para que un
        perfil DEFAULT personal no reemplace en silencio las credenciales
        de servicio del equipo."""
        resolved_config: dict[str, Any] | None = None
        # Si OCI_USER_OCID está definido, hay una intención EXPLÍCITA de usar
        # las credenciales de servicio vía variables de entorno. En ese caso,
        # un problema con la clave (faltante/ilegible) debe tratarse como un
        # error duro — nunca como una señal para caer en silencio al perfil
        # personal de ~/.oci/config, que es justo lo que esta precedencia
        # busca evitar.
        permitir_fallback_archivo = not self.config.user_ocid

        # 1. Variables de entorno primero (credenciales de servicio compartidas)
        if self.config.user_ocid:
            key_file = self.config.key_file_path
            expanded_key = os.path.expanduser(key_file) if key_file else None
            if expanded_key and os.path.isfile(expanded_key):
                resolved_config = {
                    "user": self.config.user_ocid,
                    "fingerprint": self.config.fingerprint,
                    "key_file": os.path.abspath(expanded_key),
                    "tenancy": self.config.tenancy_ocid,
                    "region": self.config.region,
                }
                self._log.info(
                    "oci_auth_using_env_vars",
                    user=self.config.user_ocid,
                    region=self.config.region,
                )
            else:
                self._log.error(
                    "oci_key_file_not_found",
                    path=self.config.key_file_path,
                    detalle=(
                        "OCI_USER_OCID está definido pero la clave no es "
                        "válida; no se intentará ~/.oci/config como respaldo "
                        "para no autenticar en silencio con un perfil distinto "
                        "al de servicio. Corrija OCI_KEY_FILE."
                    ),
                )

        # 2. Archivo de configuración como respaldo — SOLO si no hubo
        # intención explícita de usar variables de entorno (OCI_USER_OCID
        # ausente). Si estaba definido pero falló, no se cae hasta acá.
        if not resolved_config and permitir_fallback_archivo:
            target_cfg_path = self.config.config_file_path or os.path.expanduser(
                "~/.oci/config"
            )
            if os.path.isfile(target_cfg_path):
                try:
                    resolved_config = oci.config.from_file(
                        file_location=target_cfg_path,
                        profile_name=self.config.profile_name,
                    )
                    self._log.info(
                        "oci_auth_using_config_file",
                        file_path=target_cfg_path,
                        profile=self.config.profile_name,
                    )
                except Exception as e:
                    self._log.warning(
                        "oci_config_file_read_failed",
                        error=str(e),
                        file_path=target_cfg_path,
                    )

        if resolved_config:
            try:
                oci.config.validate_config(resolved_config)
                self._client = oci.object_storage.ObjectStorageClient(
                    resolved_config, timeout=_TIMEOUT_CONEXION_LECTURA
                )
                if not self._namespace:
                    self._namespace = self._client.get_namespace().data
                self._log.info(
                    "oci_client_initialized_successfully",
                    namespace=self._namespace,
                )
            except Exception as ex:
                self._log.error("oci_client_validation_error", error=str(ex))
                self._client = None
        else:
            self._log.warning(
                "oci_credentials_not_configured",
                detail="Operando en modo sin credenciales. Las subidas retornarán guardado_error.",
            )

    def check_connection(self) -> dict[str, Any]:
        """Verifica la conectividad real con OCI y la accesibilidad del bucket Always Free."""
        if not self._client:
            return {
                "conectado": False,
                "motivo": "Cliente no inicializado (credenciales ausentes o inválidas)",
            }

        start_time = time.perf_counter()
        try:
            ns = self._namespace or self._client.get_namespace().data
            self._namespace = ns
            bucket_data = self._client.get_bucket(
                namespace_name=ns, bucket_name=self.config.bucket_name
            ).data
            latencia_ms = int((time.perf_counter() - start_time) * 1000)
            self._log.info(
                "oci_connection_verified",
                namespace=ns,
                bucket=self.config.bucket_name,
                tier=bucket_data.storage_tier,
                latencia_ms=latencia_ms,
            )
            return {
                "conectado": True,
                "namespace": ns,
                "bucket": self.config.bucket_name,
                "storage_tier": bucket_data.storage_tier,
                "latencia_ms": latencia_ms,
            }
        except ServiceError as se:
            self._log.error(
                "oci_service_error_on_health_check",
                status_code=se.status,
                code=se.code,
                message=se.message,
            )
            return {
                "conectado": False,
                "status_code": se.status,
                "error_code": se.code,
                "motivo": se.message,
            }
        except Exception as e:
            self._log.error("oci_generic_health_check_error", error=str(e))
            return {"conectado": False, "motivo": str(e)}

    @staticmethod
    def calculate_sha256(data: bytes | str) -> str:
        """Calcula el hash criptográfico SHA-256 de un flujo de bytes o texto UTF-8."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def build_object_path(
        self, periodo_referencia: str, run_id: str, numero_revision: int
    ) -> str:
        """Genera la ruta inmutable determinista: activos/{periodo}/{run_id}/revision-{num:03d}.json.

        Valida cada segmento contra una lista blanca para evitar traversal
        (`../`) o separadores inesperados dentro de la ruta del objeto.
        """
        periodo = _validar_segmento_ruta(periodo_referencia, "periodo_referencia")
        run = _validar_segmento_ruta(run_id, "run_id")
        if numero_revision < 1:
            raise ValueError(
                f"numero_revision debe ser >= 1, recibido: {numero_revision}"
            )
        return f"activos/{periodo}/{run}/revision-{numero_revision:03d}.json"

    def _serializar_payload(
        self, package_payload: dict[str, Any] | str | bytes
    ) -> bytes:
        """Convierte el payload a bytes UTF-8.

        Solo admite dict, str, bytes o un modelo Pydantic con
        `model_dump_json` — cualquier otro tipo levanta `TypeError`
        explícito en vez de subir `str(obj)` en silencio.
        """
        if isinstance(package_payload, bytes):
            return package_payload
        if isinstance(package_payload, str):
            return package_payload.encode("utf-8")
        if isinstance(package_payload, dict):
            almacenamiento = package_payload.get("almacenamiento_oci")
            if isinstance(almacenamiento, dict):
                status_previo = almacenamiento.get("status_almacenamiento")
                if status_previo not in (None, "no_iniciado"):
                    self._log.warning(
                        "oci_payload_con_estado_precompuesto",
                        status_recibido=status_previo,
                        detalle=(
                            "El payload ya trae status_almacenamiento distinto de "
                            "'no_iniciado' antes de subirse; ese campo debe reflejar "
                            "el resultado de ESTA subida, no un valor precalculado "
                            "por quien construyó el paquete."
                        ),
                    )
            return json.dumps(
                package_payload,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ).encode("utf-8")
        if hasattr(package_payload, "model_dump_json"):
            return package_payload.model_dump_json(by_alias=True, indent=2).encode(
                "utf-8"
            )
        raise TypeError(
            f"Tipo de payload no soportado: {type(package_payload).__name__}. "
            "Use dict, str, bytes o un modelo Pydantic."
        )

    def upload_package(
        self,
        package_payload: dict[str, Any] | str | bytes,
        periodo_referencia: str,
        run_id: str,
        numero_revision: int = 1,
        request_id: str = "req-desconocido",
    ) -> UploadResult:
        """Sube un paquete y ejecuta de inmediato la comprobación activa de lectura.

        Es inmutable por diseño: si la ruta ya existe, solo se acepta como
        éxito idempotente cuando el contenido es exactamente el mismo;
        si difiere, se reporta como una colisión real (error), nunca se
        sobrescribe en silencio.
        """
        bound_log = self._log.bind(
            run_id=run_id, request_id=request_id, revision=numero_revision
        )
        ruta_objeto = self.build_object_path(
            periodo_referencia, run_id, numero_revision
        )

        try:
            upload_bytes = self._serializar_payload(package_payload)
        except TypeError as e:
            bound_log.error("oci_payload_no_serializable", error=str(e))
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                mensaje_error=str(e),
            )

        hash_subido = self.calculate_sha256(upload_bytes)

        if not self._client:
            bound_log.error(
                "oci_upload_failed_no_client", ruta_objeto=ruta_objeto
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                mensaje_error="Credenciales OCI no disponibles en el entorno",
            )

        content_md5 = base64.b64encode(hashlib.md5(upload_bytes).digest()).decode(
            "ascii"
        )
        start_upload = time.perf_counter()
        try:
            bound_log.info(
                "oci_upload_started",
                ruta=ruta_objeto,
                bytes_size=len(upload_bytes),
                sha256=hash_subido,
            )
            self._client.put_object(
                namespace_name=self._namespace,
                bucket_name=self.config.bucket_name,
                object_name=ruta_objeto,
                put_object_body=upload_bytes,
                content_type="application/json; charset=utf-8",
                if_none_match="*",
                content_md5=content_md5,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            )
            upload_latencia_ms = int(
                (time.perf_counter() - start_upload) * 1000
            )
            bound_log.info(
                "oci_put_object_succeeded", latencia_ms=upload_latencia_ms
            )
        except ServiceError as se:
            upload_latencia_ms = int(
                (time.perf_counter() - start_upload) * 1000
            )
            if se.status == 412:
                return self._resolver_conflicto_inmutabilidad(
                    ruta_objeto, hash_subido, upload_latencia_ms, bound_log
                )
            bound_log.error(
                "oci_put_object_failed",
                error=se.message,
                status=se.status,
                latencia_ms=upload_latencia_ms,
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                latencia_subida_ms=upload_latencia_ms,
                mensaje_error=f"Fallo durante put_object: {se.message}",
            )
        except Exception as e:
            upload_latencia_ms = int(
                (time.perf_counter() - start_upload) * 1000
            )
            bound_log.error(
                "oci_put_object_failed",
                error=str(e),
                latencia_ms=upload_latencia_ms,
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                latencia_subida_ms=upload_latencia_ms,
                mensaje_error=f"Fallo durante put_object: {e!s}",
            )

        return self._verificar_lectura(
            ruta_objeto, hash_subido, upload_latencia_ms, bound_log
        )

    def _verificar_lectura(
        self, ruta_objeto: str, hash_subido: str, upload_latencia_ms: int, bound_log
    ) -> UploadResult:
        """Comprobación Activa de Lectura (get_object - Roundtrip)."""
        start_read = time.perf_counter()
        try:
            bound_log.info("oci_verification_read_started", ruta=ruta_objeto)
            get_response = self._client.get_object(
                namespace_name=self._namespace,
                bucket_name=self.config.bucket_name,
                object_name=ruta_objeto,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            )
            retrieved_bytes = get_response.data.content
            hash_recuperado = self.calculate_sha256(retrieved_bytes)
            read_latencia_ms = int((time.perf_counter() - start_read) * 1000)

            if hash_subido == hash_recuperado:
                bound_log.info(
                    "oci_verification_read_succeeded",
                    hash_match=True,
                    latencia_ms=read_latencia_ms,
                )
                return UploadResult(
                    bucket=self.config.bucket_name,
                    ruta_objeto=ruta_objeto,
                    status_almacenamiento="guardado_con_exito",
                    comprobacion_lectura=True,
                    hash_sha256_subido=hash_subido,
                    hash_sha256_recuperado=hash_recuperado,
                    latencia_subida_ms=upload_latencia_ms,
                    latencia_lectura_ms=read_latencia_ms,
                )
            bound_log.error(
                "oci_hash_mismatch_error",
                expected=hash_subido,
                received=hash_recuperado,
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                hash_sha256_recuperado=hash_recuperado,
                latencia_subida_ms=upload_latencia_ms,
                latencia_lectura_ms=read_latencia_ms,
                mensaje_error="Discrepancia de integridad: el hash del objeto recuperado no coincide",
            )
        except Exception as e:
            read_latencia_ms = int((time.perf_counter() - start_read) * 1000)
            bound_log.error(
                "oci_verification_read_failed",
                error=str(e),
                latencia_ms=read_latencia_ms,
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                latencia_subida_ms=upload_latencia_ms,
                latencia_lectura_ms=read_latencia_ms,
                mensaje_error=f"Fallo durante get_object de verificación: {e!s}",
            )

    def _resolver_conflicto_inmutabilidad(
        self, ruta_objeto: str, hash_subido: str, upload_latencia_ms: int, bound_log
    ) -> UploadResult:
        """La ruta ya existía (412 de if_none_match).

        Como las rutas son inmutables por diseño, esto solo es válido como
        reintento idempotente si el contenido existente es EXACTAMENTE el
        mismo; si difiere, es una colisión real de nombres y se reporta
        como error en vez de dejarlo pasar.
        """
        bound_log.warning(
            "oci_object_already_exists_checking_idempotency", ruta=ruta_objeto
        )
        try:
            existing = self._client.get_object(
                namespace_name=self._namespace,
                bucket_name=self.config.bucket_name,
                object_name=ruta_objeto,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            )
            hash_existente = self.calculate_sha256(existing.data.content)
        except Exception as e:
            bound_log.error("oci_conflict_verification_failed", error=str(e))
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                hash_sha256_subido=hash_subido,
                latencia_subida_ms=upload_latencia_ms,
                mensaje_error=f"El objeto ya existía y no se pudo verificar su contenido: {e!s}",
            )

        if hash_existente == hash_subido:
            bound_log.info(
                "oci_upload_idempotent_retry_confirmed", ruta=ruta_objeto
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_con_exito",
                comprobacion_lectura=True,
                hash_sha256_subido=hash_subido,
                hash_sha256_recuperado=hash_existente,
                latencia_subida_ms=upload_latencia_ms,
                reintento_idempotente=True,
            )

        bound_log.error(
            "oci_immutability_collision",
            ruta=ruta_objeto,
            hash_nuevo=hash_subido,
            hash_existente=hash_existente,
        )
        return UploadResult(
            bucket=self.config.bucket_name,
            ruta_objeto=ruta_objeto,
            status_almacenamiento="guardado_error",
            comprobacion_lectura=False,
            hash_sha256_subido=hash_subido,
            hash_sha256_recuperado=hash_existente,
            latencia_subida_ms=upload_latencia_ms,
            mensaje_error=(
                "La ruta ya contiene una revisión inmutable con contenido distinto. "
                "Use un numero_revision nuevo en vez de reintentar sobre la misma."
            ),
        )

    def download_package(self, ruta_objeto: str) -> dict[str, Any]:
        """Descarga y deserializa un paquete de distribución persistido en OCI."""
        if not self._client:
            raise RuntimeError(
                "Cliente OCI no inicializado. Revise credenciales en .env."
            )

        start_time = time.perf_counter()
        try:
            self._log.info("oci_download_package_started", ruta=ruta_objeto)
            response = self._client.get_object(
                namespace_name=self._namespace,
                bucket_name=self.config.bucket_name,
                object_name=ruta_objeto,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            )
            content_bytes = response.data.content
            latencia_ms = int((time.perf_counter() - start_time) * 1000)

            data = json.loads(content_bytes.decode("utf-8"))
            self._log.info(
                "oci_download_package_succeeded",
                ruta=ruta_objeto,
                latencia_ms=latencia_ms,
                bytes_size=len(content_bytes),
            )
            return data
        except ServiceError as se:
            self._log.error(
                "oci_service_error_downloading",
                ruta=ruta_objeto,
                status=se.status,
                code=se.code,
                message=se.message,
            )
            raise
        except Exception as e:
            self._log.error(
                "oci_generic_download_error", ruta=ruta_objeto, error=str(e)
            )
            raise

    def retry_upload_from_disk(
        self,
        local_file_path: str | Path,
        periodo_referencia: str,
        run_id: str,
        numero_revision: int,
        request_id: str,
    ) -> UploadResult:
        """Reintenta una subida pendiente desde una exportación local en disco.

        `request_id` ahora es obligatorio: antes se perdía y los logs del
        reintento quedaban con "req-desconocido", rompiendo la trazabilidad
        del lote original.

        Valida que el archivo sea JSON bien formado antes de subirlo — el
        hash SHA-256 solo demuestra integridad de TRANSPORTE, no que el
        contenido sea un JSON válido o un PaqueteSalida coherente.
        """
        path = Path(local_file_path)
        ruta_objeto = self.build_object_path(
            periodo_referencia, run_id, numero_revision
        )

        if not path.is_file():
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                mensaje_error=f"Archivo local no encontrado para reintento: {local_file_path}",
            )

        raw_bytes = path.read_bytes()
        try:
            json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._log.error(
                "oci_retry_invalid_local_json",
                path=str(path),
                run_id=run_id,
                request_id=request_id,
                error=str(e),
            )
            return UploadResult(
                bucket=self.config.bucket_name,
                ruta_objeto=ruta_objeto,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                mensaje_error=f"El archivo local no es JSON válido, no se sube: {e}",
            )

        return self.upload_package(
            package_payload=raw_bytes,
            periodo_referencia=periodo_referencia,
            run_id=run_id,
            numero_revision=numero_revision,
            request_id=request_id,
        )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    configure_logging()
    print("=" * 70)
    print(" COMMUNITYLAB - DIAGNÓSTICO DE OCI OBJECT STORAGE (ALWAYS FREE)")
    print("=" * 70)

    client = OCIStorageClient()
    diagnostico = client.check_connection()

    if diagnostico["conectado"]:
        print(f"✅ Conectividad confirmada con OCI Namespace: '{diagnostico['namespace']}'")
        print(f"📦 Bucket Always Free: '{diagnostico['bucket']}' (Tier: {diagnostico['storage_tier']})")
        print(f"⏱️ Latencia de ping: {diagnostico['latencia_ms']} ms")

        ejecutar_prueba = os.getenv("OCI_RUN_TEST_ON_INIT", "false").lower() in (
            "true", "1", "yes",
        )
        if ejecutar_prueba:
            # Prefijo único por ejecución para no colisionar con el diagnóstico
            # de otro miembro del equipo corriendo al mismo tiempo.
            sufijo = uuid.uuid4().hex[:8]
            print(f"\n🧪 Ejecutando prueba de subida y lectura activa (Roundtrip) [{sufijo}]...")
            payload_prueba = {
                "schema_version": "1.2.0",
                "tipo_evento": "test_diagnostico_equipo_34",
                "timestamp": datetime.now(UTC).isoformat(),
                "mensaje": "Verificación técnica de persistencia inmutable",
            }
            res = client.upload_package(
                package_payload=payload_prueba,
                periodo_referencia="diagnostico",
                run_id=f"run-test-{sufijo}",
                numero_revision=1,
                request_id=f"req-diagnostico-{sufijo}",
            )
            print(f"📝 Ruta Objeto: {res.ruta_objeto}")
            print(f"📊 Estado Almacenamiento: {res.status_almacenamiento}")
            print(f"🔍 Comprobación de Lectura: {res.comprobacion_lectura}")
            print(f"⏱️ Latencias -> Subida: {res.latencia_subida_ms} ms | Lectura: {res.latencia_lectura_ms} ms")
    else:
        print("⚠️ Advertencia: No se pudo establecer conexión completa con OCI Object Storage.")
        print(f"   Motivo: {diagnostico.get('motivo')}")
        print("   Nota: Si está en desarrollo local sin credenciales, el sistema registrará")
        print("   'guardado_error' y conservará el paquete localmente en SQLite/disco sin abortar.")
    print("=" * 70)
