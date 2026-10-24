"""
Módulo de Validación e Ingesta de Datos (Data Engineering).
Aplica validación en dos capas, aislamiento de anomalías con soporte triple de llaves.

Cambios respecto a la versión anterior (revisión de integración con la API):

* `adaptar_payload_oficial` es DETERMINISTA: el mismo payload produce siempre
  el mismo `request_id` y las mismas fechas de respaldo. Antes usaba `uuid4()`
  y la hora de recepción, así que un reintento legítimo generaba un lote
  distinto (otro `run_id`) o un 409.
* Tolera `null` explícitos (`"autor": null`) con los mismos valores por defecto
  que una clave ausente, y convierte `id` numéricos a texto.
* Una sola advertencia agregada por lote (`fechas_origen_fabricadas`), en vez
  de una por mensaje.
* `validar_lote_crudo` rechaza ids repetidos dentro del lote (la segunda
  aparición en adelante). `source_id` es la referencia exacta del Contrato 2:
  dos mensajes con el mismo id serían indistinguibles aguas abajo.
"""
import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Any

import structlog
from pydantic import ValidationError

from esquemas import IngestionLote, InteraccionValidada, MetadataOrigenValidado

logger = structlog.get_logger("data_pipeline.validador")

# Fecha de respaldo cuando el periodo no permite derivar una (ver _fecha_de_respaldo).
_FECHA_SENTINELA = "1970-01-01T00:00:00+00:00"
_SEMANA_ISO = re.compile(r"^(\d{4})-W(\d{2})$")
_MES = re.compile(r"^(\d{4})-(\d{2})$")


def _fecha_de_respaldo(periodo_referencia: str) -> str:
    """Fecha ISO-8601 determinista derivada del periodo (nunca del reloj).

    `2026-W42` -> lunes de esa semana ISO; `2026-10` -> primer día del mes;
    `2026-10-12` -> ese día. Cualquier otra cosa -> sentinela del epoch.
    Es un valor FABRICADO: el adaptador lo reporta en el log agregado.
    """
    texto = (periodo_referencia or "").strip()
    try:
        m = _SEMANA_ISO.match(texto)
        if m:
            dia = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        elif _MES.match(texto):
            dia = date.fromisoformat(f"{texto}-01")
        else:
            dia = date.fromisoformat(texto)
        return datetime(dia.year, dia.month, dia.day, tzinfo=UTC).isoformat()
    except ValueError:
        return _FECHA_SENTINELA


def _request_id_determinista(payload_crudo: dict) -> str:
    """`req_<12 hex>` derivado del contenido: el mismo payload -> el mismo id."""
    canonico = json.dumps(payload_crudo, sort_keys=True, ensure_ascii=False, default=str)
    return "req_" + hashlib.sha256(canonico.encode("utf-8")).hexdigest()[:12]


def _texto_o_none(valor: Any) -> str | None:
    """Normaliza un id nativo: None/"" -> None; cualquier otro valor -> str."""
    if valor is None or valor == "":
        return None
    return str(valor)


def adaptar_payload_oficial(payload_crudo: dict) -> dict:
    """Transforma el JSON del jurado al contrato IngestionLote mitigando colisiones.

    Determinista e idempotente: no consulta el reloj ni genera aleatorios.
    """
    origen = payload_crudo.get("origen_comunidad") or "Desconocido"
    # Extrae el primer elemento (ej. 'Discord') en lugar de la lista completa
    plataforma = origen.split("_")[0] if "_" in origen else origen
    periodo = payload_crudo.get("periodo_referencia") or "Sin_Periodo"
    fecha_respaldo = _fecha_de_respaldo(periodo)
    request_id = payload_crudo.get("request_id") or _request_id_determinista(payload_crudo)

    interacciones_adaptadas = []
    ids_con_fecha_fabricada: list[str] = []
    for posicion, item in enumerate(payload_crudo.get("interacciones", []), start=1):
        id_nativo = _texto_o_none(item.get("id"))
        id_secuencial = id_nativo or f"{request_id}_msg_{posicion:03d}"

        meta_in = item.get("metadata_origen")
        if not isinstance(meta_in, dict):
            meta_in = {}
        fecha_original = meta_in.get("fecha")

        if fecha_original is None:
            ids_con_fecha_fabricada.append(id_secuencial)
        fecha_efectiva = fecha_original or fecha_respaldo

        interacciones_adaptadas.append({
            "id": id_secuencial,
            "autor": item.get("autor") or "Anónimo",
            "canal": item.get("canal") or "sin_canal",
            "tipo": item.get("tipo") or "sin_clasificar",
            "texto": item.get("texto") or "",
            "metadata_origen": {
                "plataforma": meta_in.get("plataforma") or plataforma,
                "identificador_original": meta_in.get("identificador_original")
                or id_nativo
                or f"autogenerado_{id_secuencial}",
                "fecha": fecha_efectiva,
            },
        })

    if ids_con_fecha_fabricada:
        logger.warning(
            "fechas_origen_fabricadas",
            request_id=request_id,
            total=len(ids_con_fecha_fabricada),
            ejemplo_ids=ids_con_fecha_fabricada[:3],
            fecha_usada=fecha_respaldo,
            motivo="metadata_origen.fecha ausente; se usó una fecha derivada del periodo",
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
    """Valida registro por registro aislando descartes con compatibilidad absoluta para el frontend."""
    validos: list[InteraccionValidada] = []
    rechazados: list[dict[str, Any]] = []
    # Ids ya ACEPTADOS: un duplicado de un registro rechazado no cuenta como duplicado.
    ids_aceptados: set[str] = set()



    for raw_item in payload.interacciones:

        try:
            if raw_item.id in ids_aceptados:
                raise ValueError(
                    f"ID duplicado dentro del lote: '{raw_item.id}' ya fue aceptado; "
                    "se conserva la primera aparición."
                )

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
            ids_aceptados.add(raw_item.id)

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
                "fecha_aislamiento": datetime.now(UTC).isoformat(),
            })



    return validos, rechazados
