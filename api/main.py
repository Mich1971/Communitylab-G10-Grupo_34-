"""API perimetral de CommunityLab (FastAPI).

Responsabilidad única: guardia de seguridad del sistema. Autentica, valida el
sobre `IngestionLote` (con soporte opcional para adaptar el JSON plano del jurado
cuando no es un sobre incompleto), aplica idempotencia y deposita el lote de
forma atómica en `data/raw/`. NO ejecuta inferencia ni lanza tareas en memoria:
el runner local detecta el archivo y orquesta el pipeline.

Endpoints
---------
POST /api/v1/interactions/process   202 nuevo | 200 duplicado idéntico |
                                    401 | 409 | 413 | 422 | 503
GET  /api/v1/runs/{run_id}          estado de un run (n8n / Streamlit)
GET  /health                        sin autenticación

Arranque (falla si falta COMMUNITYLAB_WEBHOOK_SECRET):
    uvicorn api.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from api.config import Settings
from api.logging_config import configurar_logging
from api.registro import (
    ConflictoIdempotencia,
    RegistroIdempotencia,
    ResultadoRegistro,
)
from data_pipeline.validador import adaptar_payload_oficial
from esquemas import IngestionLote

log = structlog.get_logger("api")

# Mismo criterio que storage/oci_client.py: estos valores terminan en rutas.
_SEGMENTO_VALIDO = re.compile(r"^[A-Za-z0-9._-]+$")
_RUN_ID_VALIDO = re.compile(r"^run-\d{8}T\d{6}Z-[0-9a-f]{12}$")
_MAX_LARGO_ID = 200


class AcuseRecibo(BaseModel):
    request_id: str
    run_id: str
    estado: str
    duplicado: bool = False


class EstadoRun(BaseModel):
    run_id: str
    request_id: str
    estado: str
    recibido_en: str
    actualizado_en: str


def huella_lote(lote: IngestionLote) -> str:
    """SHA-256 del JSON canónico del lote ya validado."""
    canonico = json.dumps(
        lote.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def escribir_lote_atomico(raw_dir: Path, run_id: str, lote: IngestionLote) -> Path:
    """Escribe `data/raw/{run_id}.json` sin que el runner vea nunca un archivo a medias."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    destino = raw_dir / f"{run_id}.json"
    temporal = raw_dir / f"temp_{run_id}_{uuid.uuid4().hex[:8]}.json"
    try:
        with open(temporal, "w", encoding="utf-8") as f:
            f.write(lote.model_dump_json(by_alias=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, destino)
    except BaseException:
        temporal.unlink(missing_ok=True)
        raise
    return destino


def _es_payload_plano_jurado(datos: Any) -> bool:
    """CORRECCIÓN (Riesgo 1): Detecta si un diccionario JSON corresponde al
    formato plano oficial del jurado (tiene `origen_comunidad`, `periodo_referencia`
    e `interacciones` no vacías donde ningún ítem trae `id` ni `metadata_origen`),
    distinguiéndolo de un sobre `IngestionLote` al que simplemente le borraron
    `request_id` por error.
    """
    if not isinstance(datos, dict):
        return False
    if "request_id" in datos or "schema_version" in datos:
        return False
    interacciones = datos.get("interacciones")
    if not isinstance(interacciones, list) or not interacciones:
        return False
    if not isinstance(datos.get("origen_comunidad"), str) or not isinstance(
        datos.get("periodo_referencia"), str
    ):
        return False
    return all(
        isinstance(item, dict) and "id" not in item and "metadata_origen" not in item
        for item in interacciones
    )


def _parsear_o_adaptar_lote(cuerpo: bytes) -> IngestionLote:
    """Valida contra `IngestionLote` o adapta automáticamente si es el JSON plano del jurado."""
    try:
        return IngestionLote.model_validate_json(cuerpo)
    except ValidationError as exc_original:
        try:
            crudo = json.loads(cuerpo)
        except Exception:
            raise exc_original from None
        if _es_payload_plano_jurado(crudo):
            adaptado = adaptar_payload_oficial(crudo)
            return IngestionLote.model_validate(adaptado)
        raise exc_original


def _error(
    status: int, codigo: str, detalle: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": codigo, "detalle": detalle}, headers=headers
    )


def _errores_422(exc: ValidationError) -> list[dict[str, Any]]:
    # Sin `input`/`ctx`: el valor rechazado puede contener texto de miembros (PII).
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


def _problema_de_identificadores(lote: IngestionLote) -> str | None:
    rid = lote.request_id
    if not rid.strip() or len(rid) > _MAX_LARGO_ID or any(ord(c) < 32 for c in rid):
        return f"request_id debe tener entre 1 y {_MAX_LARGO_ID} caracteres sin control"
    p = lote.periodo_referencia
    if len(p) > _MAX_LARGO_ID or not _SEGMENTO_VALIDO.match(p) or p in {".", ".."}:
        return "periodo_referencia solo admite letras, números, '.', '_' y '-'"
    return None


async def _leer_cuerpo_limitado(request: Request, maximo: int) -> bytes | None:
    """Lee el cuerpo abortando al pasar `maximo`. None = excedido."""
    declarado = request.headers.get("content-length")
    if declarado and declarado.isdigit() and int(declarado) > maximo:
        return None
    partes: list[bytes] = []
    total = 0
    async for trozo in request.stream():
        total += len(trozo)
        if total > maximo:
            return None
        partes.append(trozo)
    return b"".join(partes)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()  # falla aquí si falta el secreto
    configurar_logging(cfg.log_level)
    registro = RegistroIdempotencia(cfg.db_path, gracia_recepcion_s=cfg.gracia_recepcion_s)
    secreto = cfg.webhook_secret.encode("utf-8")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(registro.inicializar)
        cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        log.info("api_iniciada", raw_dir=str(cfg.raw_dir))
        yield

    app = FastAPI(
        title="CommunityLab API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if cfg.habilitar_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if cfg.habilitar_docs else None,
    )
    app.state.settings = cfg
    app.state.registro = registro

    def autenticado(valor: str | None) -> bool:
        return valor is not None and secrets.compare_digest(valor.encode("utf-8"), secreto)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        try:
            await asyncio.to_thread(registro.comprobar)
        except Exception:  # noqa: BLE001 - cualquier fallo de la base = no saludable
            log.error("health_db_fallo")
            return _error(503, "db_no_disponible", "La base de datos no responde")
        return JSONResponse({"status": "ok"})

    @app.post(
        "/api/v1/interactions/process",
        status_code=202,
        response_model=AcuseRecibo,
        responses={
            200: {
                "model": AcuseRecibo,
                "description": "Duplicado idéntico: devuelve el run_id existente",
            },
            401: {"description": "X-Webhook-Secret ausente o inválido"},
            409: {"description": "request_id conocido con contenido distinto"},
            413: {"description": "Cuerpo demasiado grande"},
            422: {"description": "Sobre inválido"},
            503: {"description": "Recepción idéntica en curso o fallo de escritura; reintentar"},
        },
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": IngestionLote.model_json_schema()}},
            }
        },
    )
    async def ingestar(
        request: Request, x_webhook_secret: str | None = Header(default=None)
    ) -> JSONResponse:
        # 1) Autenticar ANTES de leer el cuerpo.
        if not autenticado(x_webhook_secret):
            log.warning("auth_fallida", ruta=request.url.path)
            return _error(401, "no_autorizado", "X-Webhook-Secret ausente o inválido")

        # 2) Cuerpo acotado y sobre validado (o adaptado si es el JSON plano del jurado).
        cuerpo = await _leer_cuerpo_limitado(request, cfg.max_body_bytes)
        if cuerpo is None:
            return _error(413, "cuerpo_demasiado_grande", f"Máximo {cfg.max_body_bytes} bytes")
        try:
            lote = _parsear_o_adaptar_lote(cuerpo)
        except ValidationError as exc:
            return JSONResponse(
                status_code=422, content={"error": "sobre_invalido", "detalle": _errores_422(exc)}
            )
        problema = _problema_de_identificadores(lote)
        if problema:
            return _error(422, "identificadores_invalidos", problema)

        # 3) Idempotencia.
        huella = huella_lote(lote)
        try:
            res: ResultadoRegistro = await asyncio.to_thread(
                registro.registrar, lote.request_id, huella
            )
        except ConflictoIdempotencia:
            log.warning("ingesta_conflicto", request_id=lote.request_id)
            return _error(
                409,
                "conflicto_idempotencia",
                "request_id ya registrado con contenido distinto; no se sobrescribe",
            )

        ligado = log.bind(request_id=lote.request_id, run_id=res.run_id)

        if res.accion == "repetido":
            ligado.info("ingesta_duplicada", estado=res.estado)
            return JSONResponse(
                status_code=200,
                content=AcuseRecibo(
                    request_id=lote.request_id,
                    run_id=res.run_id,
                    estado=res.estado,
                    duplicado=True,
                ).model_dump(),
            )
        if res.accion == "en_curso":
            ligado.info("ingesta_en_curso")
            return _error(
                503,
                "recepcion_en_curso",
                "Una petición idéntica se está recibiendo; reintentar en unos segundos",
                headers={"Retry-After": "5"},
            )

        # 4) nuevo | retomar: depositar el archivo y marcar en cola.
        try:
            await asyncio.to_thread(escribir_lote_atomico, cfg.raw_dir, res.run_id, lote)
            await asyncio.to_thread(registro.marcar_en_cola, res.run_id)
        except Exception:  # noqa: BLE001 - fallo de disco/DB: no dejar run fantasma
            ligado.exception("ingesta_escritura_fallida")
            if res.accion == "nuevo":
                try:
                    await asyncio.to_thread(registro.descartar_recepcion, res.run_id)
                except Exception:  # noqa: BLE001 - quedará 'recibiendo' y se retomará
                    ligado.error("descarte_recepcion_fallido")
            return _error(
                503,
                "escritura_fallida",
                "No se pudo persistir el lote; reintentar",
                headers={"Retry-After": "5"},
            )

        ligado.info(
            "ingesta_recibida",
            accion=res.accion,
            interacciones=len(lote.interacciones),
            hash=huella[:12],
        )
        return JSONResponse(
            status_code=202,
            content=AcuseRecibo(
                request_id=lote.request_id, run_id=res.run_id, estado="en_cola"
            ).model_dump(),
        )

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=EstadoRun,
        responses={401: {}, 404: {}},
    )
    async def estado_run(
        run_id: str, x_webhook_secret: str | None = Header(default=None)
    ) -> JSONResponse:
        if not autenticado(x_webhook_secret):
            return _error(401, "no_autorizado", "X-Webhook-Secret ausente o inválido")
        if not _RUN_ID_VALIDO.match(run_id):
            return _error(404, "run_no_encontrado", "run_id desconocido")
        fila = await asyncio.to_thread(registro.obtener_por_run_id, run_id)
        if fila is None:
            return _error(404, "run_no_encontrado", "run_id desconocido")
        return JSONResponse(
            EstadoRun(
                run_id=fila.run_id,
                request_id=fila.request_id,
                estado=fila.estado,
                recibido_en=fila.recibido_en,
                actualizado_en=fila.actualizado_en,
            ).model_dump()
        )

    return app


def __getattr__(nombre: str) -> FastAPI:
    if nombre == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(nombre)