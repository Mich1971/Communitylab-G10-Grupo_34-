"""
Módulo de Ingesta y Validación (Data Engineering) - CommunityLab MVP.
Aplica validación determinista de dos capas alineada con esquemas.py.
"""

import hashlib
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError

from esquemas import IngestionLote, InteraccionValidada, MetadataOrigenValidado

logger = structlog.get_logger("data_pipeline.validador")


def _derivar_fecha_respaldo(periodo: str) -> str:
    """Deriva una fecha base determinista basada en el periodo de referencia."""
    try:
        if "-W" in periodo:
            parts = periodo.split("-W")
            year = int(parts[0])
            week = int(parts[1])
            return (
                datetime.fromisocalendar(year, week, 1)
                .replace(tzinfo=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
        if len(periodo) >= 7 and "-" in periodo:
            year = int(periodo[:4])
            month = int(periodo[5:7])
            return (
                datetime(year, month, 1, tzinfo=UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
    except ValueError as e:
        # CORRECCIÓN RUFF: Se registra el error en modo debug en lugar de usar un pass ciego
        logger.debug(
            "error_parseo_periodo_fecha_respaldo",
            periodo=periodo,
            error=str(e),
        )
    return "1970-01-01T00:00:00Z"


def adaptar_payload_oficial(payload_crudo: dict) -> dict:
    """Transforma el JSON crudo del jurado al contrato IngestionLote de forma determinista."""
    origen = payload_crudo.get("origen_comunidad", "Desconocido")
    plataforma = origen.split("_") if "_" in origen else origen

    periodo = payload_crudo.get("periodo_referencia", "Sin_Periodo")
    fecha_respaldo = _derivar_fecha_respaldo(periodo)

    if payload_crudo.get("request_id"):
        request_id = payload_crudo["request_id"]
    else:
        contenido_str = str(payload_crudo.get("interacciones", []))
        hash_req = hashlib.sha256(contenido_str.encode("utf-8")).hexdigest()
        request_id = f"req_{hash_req[:12]}"

    interacciones_adaptadas = []
    fechas_fabricadas_ids = []

    for posicion, item in enumerate(
        payload_crudo.get("interacciones", []), start=1
    ):
        id_raw = item.get("id")
        id_secuencial = (
            str(id_raw)
            if id_raw is not None
            else f"{request_id}_msg_{posicion:03d}"
        )

        meta_in = item.get("metadata_origen") or {}
        fecha_original = meta_in.get("fecha")

        if not fecha_original:
            fechas_fabricadas_ids.append(id_secuencial)
            fecha_efectiva = fecha_respaldo
        else:
            fecha_efectiva = str(fecha_original)

        sobre_individual = {
            "id": id_secuencial,
            "autor": (
                str(item["autor"]) if item.get("autor") is not None else "Anónimo"
            ),
            "canal": (
                str(item["canal"])
                if item.get("canal") is not None
                else "sin_canal"
            ),
            "tipo": (
                str(item["tipo"])
                if item.get("tipo") is not None
                else "sin_clasificar"
            ),
            "texto": (
                str(item["texto"]) if item.get("texto") is not None else ""
            ),
            "metadata_origen": {
                "plataforma": meta_in.get("plataforma") or plataforma,
                "identificador_original": (
                    meta_in.get("identificador_original") or id_secuencial
                ),
                "fecha": fecha_efectiva,
            },
        }
        interacciones_adaptadas.append(sobre_individual)

    if fechas_fabricadas_ids:
        logger.warning(
            "fechas_origen_fabricadas",
            request_id=request_id,
            total=len(fechas_fabricadas_ids),
            ejemplos=fechas_fabricadas_ids[:3],
        )

    return {
        "schema_version": payload_crudo.get("schema_version", "1.1.0"),
        "request_id": request_id,
        "origen_comunidad": origen,
        "periodo_referencia": periodo,
        "interacciones": interacciones_adaptadas,
    }


def validar_lote_crudo(
    payload: IngestionLote,
    run_id: str | None = None,
) -> tuple[list[InteraccionValidada], list[dict[str, Any]]]:
    """Valida registro por registro controlando y aislando duplicaciones internas del lote."""
    validos: list[InteraccionValidada] = []
    rechazados: list[dict[str, Any]] = []
    ids_procesados = set()

    for raw_item in payload.interacciones:
        try:
            if raw_item.id in ids_procesados:
                raise ValueError(
                    f"ID duplicado detectado dentro del lote: {raw_item.id}"
                )

            if not raw_item.texto or not raw_item.texto.strip():
                raise ValueError(
                    "El texto de la interacción no puede estar vacío ni contener solo espacios"
                )

            meta_validada = MetadataOrigenValidado(
                plataforma=raw_item.metadata_origen.plataforma,
                identificador_original=raw_item.metadata_origen.identificador_original,
                fecha=raw_item.metadata_origen.fecha,
            )

            validated = InteraccionValidada(
                id=raw_item.id,
                autor=raw_item.autor,
                canal=raw_item.canal,
                tipo=raw_item.tipo,
                texto=raw_item.texto.strip(),
                metadata_origen=meta_validada,
            )
            validos.append(validated)
            ids_procesados.add(raw_item.id)

        except (ValidationError, ValueError) as err:
            motivo_error = str(err)
            logger.warning(
                "registro_crudo_rechazado",
                request_id=payload.request_id,
                run_id=run_id,
                mensaje_id=raw_item.id,
                error=motivo_error,
            )

            rechazados.append({
                "id": raw_item.id,
                "mensaje_id": raw_item.id,
                "error": motivo_error,
                "error_detalle": motivo_error,
                "run_id": run_id,
                "request_id": payload.request_id,
                "autor": raw_item.autor,
                "canal": raw_item.canal,
                "texto_crudo": raw_item.texto,
                "fecha_aislamiento": (
                    datetime.now(UTC).isoformat().replace("+00:00", "Z")
                ),
            })

    return validos, rechazados
