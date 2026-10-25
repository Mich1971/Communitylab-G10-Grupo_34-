"""Rutas HTTP de curaduría y revisiones (operaciones 1 a 10 de la sección 2 del Plan).

Cada ruta es un envoltorio fino sobre `storage.consultas` y `storage.revisiones`:
mismos nombres, parámetros y modelos de respuesta Pydantic v2.

Reglas transversales:
* Autenticación estricta con `X-Webhook-Secret`: cualquier petición sin cabecera
  válida devuelve HTTP 401 `{"error": "no_autorizado", "detalle": "..."}`.
* Errores de dominio tipados: `ErrorCuraduria` se traduce automáticamente a
  `{"error": exc.codigo, "detalle": exc.detalle}` (más `revision_actual` en 409).
* Errores de validación de FastAPI (`RequestValidationError`) se formatean con
  el estándar canónico `{"error": "parametro_invalido" | "revision_invalida", "detalle": "..."}`.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from storage import consultas as c
from storage import revisiones as r
from storage.consultas import ConflictoRevision, ErrorCuraduria
from storage.modelos_curaduria import (
    RespuestaDetalleEjecucion,
    RespuestaGuardarOCI,
    RespuestaListarEjecuciones,
    RespuestaListarMensajes,
    RespuestaListarRechazados,
    RespuestaListarRevisiones,
    RespuestaObtenerFuentes,
    RespuestaObtenerResumen,
    RespuestaObtenerRevision,
    RespuestaRegistrarRevision,
    SolicitudRegistrarRevision,
)


class NoAutorizadoError(Exception):
    """Falta o no coincide la cabecera X-Webhook-Secret."""

    detalle = "X-Webhook-Secret ausente o inválido"


def _verificar_autenticacion(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    cfg = getattr(request.app.state, "settings", None)
    secreto = cfg.webhook_secret.encode("utf-8") if cfg else None
    if not (
        x_webhook_secret
        and secreto
        and secrets.compare_digest(x_webhook_secret.encode("utf-8"), secreto)
    ):
        raise NoAutorizadoError()


def _db_path(request: Request) -> str | None:
    """Extrae la ruta de base de datos de la configuración activa en la app."""
    cfg = getattr(request.app.state, "settings", None)
    return str(cfg.db_path) if cfg and getattr(cfg, "db_path", None) else None


def registrar_manejadores_error(app: FastAPI) -> None:
    """Registra los convertidores de errores de curaduría al formato canónico {"error", "detalle"}."""

    @app.exception_handler(NoAutorizadoError)
    async def no_autorizado_handler(_req: Request, exc: NoAutorizadoError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": "no_autorizado", "detalle": exc.detalle},
        )

    @app.exception_handler(ErrorCuraduria)
    async def error_curaduria_handler(_req: Request, exc: ErrorCuraduria) -> JSONResponse:
        contenido: dict[str, Any] = {"error": exc.codigo, "detalle": exc.detalle}
        if isinstance(exc, ConflictoRevision):
            contenido["revision_actual"] = exc.revision_actual
        return JSONResponse(status_code=exc.http_status, content=contenido)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(req: Request, exc: RequestValidationError) -> JSONResponse:
        # Si la petición apunta al endpoint de registrar revisión, el error es revision_invalida
        es_revision = req.url.path.endswith("/revisiones") and req.method == "POST"
        codigo = "revision_invalida" if es_revision else "parametro_invalido"
        partes = [f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', '')}" for err in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={"error": codigo, "detalle": "; ".join(partes)},
        )


router = APIRouter(prefix="/api/v1", tags=["curaduria"], dependencies=[Depends(_verificar_autenticacion)])


# ---------------------------------------------------------------------------
# 1. listar_ejecuciones (GET /api/v1/runs)
# ---------------------------------------------------------------------------
@router.get("/runs", response_model=RespuestaListarEjecuciones)
async def listar_ejecuciones_endpoint(
    request: Request,
    limite: int = 20,
    cursor: str | None = None,
    estado: str | None = None,
    periodo: str | None = None,
    comunidad: str | None = None,
) -> RespuestaListarEjecuciones:
    return await asyncio.to_thread(
        c.listar_ejecuciones,
        limite=limite,
        cursor=cursor,
        estado=estado,
        periodo=periodo,
        comunidad=comunidad,
        db_path=_db_path(request),
    )


# ---------------------------------------------------------------------------
# 2. obtener_ejecucion (GET /api/v1/runs/{run_id}/detalle)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/detalle", response_model=RespuestaDetalleEjecucion)
async def obtener_ejecucion_endpoint(
    run_id: str,
    request: Request,
) -> RespuestaDetalleEjecucion:
    return await asyncio.to_thread(c.obtener_ejecucion, run_id, db_path=_db_path(request))


# ---------------------------------------------------------------------------
# 3. listar_mensajes (GET /api/v1/runs/{run_id}/mensajes)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/mensajes", response_model=RespuestaListarMensajes)
async def listar_mensajes_endpoint(
    run_id: str,
    request: Request,
    apto: bool | None = None,
    categoria: str | None = None,
    sentimiento: str | None = None,
    requiere_soporte: bool | None = None,
    limite: int = 50,
    cursor: str | None = None,
) -> RespuestaListarMensajes:
    return await asyncio.to_thread(
        c.listar_mensajes,
        run_id,
        apto=apto,
        categoria=categoria,
        sentimiento=sentimiento,
        requiere_soporte=requiere_soporte,
        limite=limite,
        cursor=cursor,
        db_path=_db_path(request),
    )


# ---------------------------------------------------------------------------
# 4. obtener_fuentes (GET /api/v1/runs/{run_id}/fuentes)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/fuentes", response_model=RespuestaObtenerFuentes)
async def obtener_fuentes_endpoint(
    run_id: str,
    request: Request,
    source_ids: Annotated[list[str], Query()] = (),
) -> RespuestaObtenerFuentes:
    # Admite tanto ?source_ids=a,b como ?source_ids=a&source_ids=b
    lista_ids: list[str] = []
    for item in source_ids:
        for s in item.split(","):
            s_limpio = s.strip()
            if s_limpio:
                lista_ids.append(s_limpio)
    return await asyncio.to_thread(
        c.obtener_fuentes, run_id, lista_ids, db_path=_db_path(request)
    )


# ---------------------------------------------------------------------------
# 5. obtener_resumen (GET /api/v1/runs/{run_id}/resumen)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/resumen", response_model=RespuestaObtenerResumen)
async def obtener_resumen_endpoint(
    run_id: str,
    request: Request,
) -> RespuestaObtenerResumen:
    return await asyncio.to_thread(c.obtener_resumen, run_id, db_path=_db_path(request))


# ---------------------------------------------------------------------------
# 6. listar_rechazados (GET /api/v1/runs/{run_id}/rechazados)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/rechazados", response_model=RespuestaListarRechazados)
async def listar_rechazados_endpoint(
    run_id: str,
    request: Request,
    origen: str | None = None,
    incluir_texto: bool = False,
) -> RespuestaListarRechazados:
    return await asyncio.to_thread(
        c.listar_rechazados,
        run_id,
        origen=origen,
        incluir_texto=incluir_texto,
        db_path=_db_path(request),
    )


# ---------------------------------------------------------------------------
# 7. listar_revisiones (GET /api/v1/runs/{run_id}/revisiones)
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/revisiones", response_model=RespuestaListarRevisiones)
async def listar_revisiones_endpoint(
    run_id: str,
    request: Request,
) -> RespuestaListarRevisiones:
    return await asyncio.to_thread(c.listar_revisiones, run_id, db_path=_db_path(request))


# ---------------------------------------------------------------------------
# 8. obtener_revision (GET /api/v1/runs/{run_id}/revisiones/{n})
# ---------------------------------------------------------------------------
@router.get("/runs/{run_id}/revisiones/{n}", response_model=RespuestaObtenerRevision)
async def obtener_revision_endpoint(
    run_id: str,
    n: int,
    request: Request,
) -> RespuestaObtenerRevision:
    return await asyncio.to_thread(c.obtener_revision, run_id, n, db_path=_db_path(request))


# ---------------------------------------------------------------------------
# 9. registrar_revision (POST /api/v1/runs/{run_id}/revisiones)
# ---------------------------------------------------------------------------
@router.post("/runs/{run_id}/revisiones", status_code=201, response_model=RespuestaRegistrarRevision)
async def registrar_revision_endpoint(
    run_id: str,
    body: SolicitudRegistrarRevision,
    request: Request,
) -> RespuestaRegistrarRevision:
    return await asyncio.to_thread(
        r.registrar_revision,
        run_id,
        body.revision_base,
        body.edicion,
        db_path=_db_path(request),
    )


# ---------------------------------------------------------------------------
# 10. guardar_revision_en_oci (POST /api/v1/runs/{run_id}/revisiones/{n}/oci)
# ---------------------------------------------------------------------------
@router.post("/runs/{run_id}/revisiones/{n}/oci", response_model=RespuestaGuardarOCI)
async def guardar_revision_en_oci_endpoint(
    run_id: str,
    n: int,
    request: Request,
) -> RespuestaGuardarOCI:
    return await asyncio.to_thread(
        r.guardar_revision_en_oci,
        run_id,
        n,
        cliente=None,
        db_path=_db_path(request),
    )
