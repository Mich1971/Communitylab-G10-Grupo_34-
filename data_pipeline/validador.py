"""
Módulo de Validación e Ingesta de Datos (Data Engineering).
Aplica validación en dos capas, aislamiento de anomalías con soporte triple de llaves.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import uuid
from pydantic import ValidationError
import structlog

from esquemas import IngestionLote, InteraccionValidada, MetadataOrigenValidado

logger = structlog.get_logger("data_pipeline.validador")


def adaptar_payload_oficial(payload_crudo: dict) -> dict:
    """Transforma el JSON del jurado al contrato IngestionLote mitigando colisiones."""
    origen = payload_crudo.get("origen_comunidad", "Desconocido")
    # CORRECCIÓN: Extrae el primer elemento (ej. 'Discord') en lugar de la lista completa
    plataforma = origen.split("_")[0] if "_" in origen else origen
    fecha_ingesta = datetime.now(timezone.utc).isoformat()
    request_id = payload_crudo.get("request_id") or f"req_{uuid.uuid4().hex[:12]}"

    interacciones_adaptadas = []
    for posicion, item in enumerate(payload_crudo.get("interacciones", []), start=1):
        id_nativo = item.get("id")
        id_secuencial = id_nativo or f"{request_id}_msg_{posicion:03d}"

        meta_in = item.get("metadata_origen", {})
        fecha_original = meta_in.get("fecha")

        if fecha_original is None:
            logger.warning(
                "fecha_origen_fabricada",
                request_id=request_id,
                mensaje_id=id_secuencial,
                motivo="metadata_origen.fecha ausente; se usó fecha de ingesta",
            )
        fecha_efectiva = fecha_original or fecha_ingesta

        sobre_individual = {
            "id": id_secuencial,
            "autor": item.get("autor", "Anónimo"),
            "canal": item.get("canal", "sin_canal"),
            "tipo": item.get("tipo", "sin_clasificar"),
            "texto": item.get("texto", ""),
            "metadata_origen": {
                "plataforma": meta_in.get("plataforma", plataforma),
                "identificador_original": meta_in.get("identificador_original", id_nativo or f"autogenerado_{id_secuencial}"),
                "fecha": fecha_efectiva,
            },
        }
        interacciones_adaptadas.append(sobre_individual)

    return {
        "schema_version": payload_crudo.get("schema_version", "1.1.0"),
        "request_id": request_id,
        "origen_comunidad": origen,
        "periodo_referencia": payload_crudo.get("periodo_referencia", "Sin_Periodo"),
        "interacciones": interacciones_adaptadas,
    }


def validar_lote_crudo(
    payload: IngestionLote,
    run_id: Optional[str] = None,
) -> Tuple[List[InteraccionValidada], List[Dict[str, Any]]]:
    """Valida registro por registro aislando descartes con compatibilidad absoluta para el frontend."""
    validos: List[InteraccionValidada] = []
    rechazados: List[Dict[str, Any]] = []

    for raw_item in payload.interacciones:
        try:
            # OPTIMIZACIÓN: Rechaza textos vacíos o con puros espacios para ahorrar tokens
            if not raw_item.texto or not raw_item.texto.strip():
                raise ValueError("El texto de la interacción está vacío o contiene solo espacios.")

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
                texto=raw_item.texto,
                metadata_origen=meta_validada,
            )
            validos.append(validated)

        except (ValidationError, ValueError) as err:
            motivo_error = str(err)
            logger.warning(
                "registro_crudo_rechazado",
                request_id=payload.request_id,
                run_id=run_id,
                mensaje_id=raw_item.id,
                error=motivo_error,
            )

            # SOPORTE INTEGRAL: Une aserciones del jurado, ui de Sergio y persistencia SQLite
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
                "fecha_aislamiento": datetime.now(timezone.utc).isoformat(),
            })

    return validos, rechazados
