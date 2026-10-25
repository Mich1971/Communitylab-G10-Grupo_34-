"""Escrituras de la curaduría: nueva revisión inmutable y guardado verificado en OCI.

Reglas que comparten ambas operaciones:

* Una revisión nunca se modifica: cada acción del panel inserta `revision-N+1` a
  partir de la base indicada (`_insertar_paquete(..., sobrescribir=False)`).
* El control optimista (`revision_base`) se evalúa *dentro* de la transacción
  `BEGIN IMMEDIATE`, de modo que dos sesiones con la misma base no pueden ganar
  ambas: la segunda recibe `ConflictoRevision` y no escribe nada.
* `guardado_con_exito` solo lo decide el cliente OCI tras `put_object` + `get_object`
  con comparación SHA-256; aquí nunca se asume.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from esquemas import (
    EstadoRevision,
    EvidenciaAlmacenamientoOCI,
    PaqueteSalida,
    fecha_utc_ahora,
)
from orchestration.invariantes import violaciones_trazabilidad_por_ids
from storage.consultas import (
    ConflictoRevision,
    EscrituraFallida,
    RevisionInvalida,
    RevisionNoEncontrada,
    _lectura,
    _resolver_run,
)
from storage.local_db import (
    _insertar_paquete,
    _transaccion,
    actualizar_evidencia_oci,
    reconstruir_json_paquete_salida,
)
from storage.modelos_curaduria import (
    EdicionRevisionInput,
    RespuestaGuardarOCI,
    RespuestaRegistrarRevision,
)
from storage.oci_client import OCIStorageClient, _validar_segmento_ruta

logger = structlog.get_logger("storage.revisiones")


def _ruta_objeto(periodo: str, run_id: str, numero_revision: int) -> str:
    """Misma ruta inmutable que calcula el cliente OCI, sin instanciarlo (exigiría credenciales)."""
    try:
        periodo, run_id = _validar_segmento_ruta(periodo, "periodo"), _validar_segmento_ruta(run_id, "run_id")
    except ValueError as exc:
        raise EscrituraFallida(str(exc)) from exc
    return f"activos/{periodo}/{run_id}/revision-{numero_revision:03d}.json"


def _validar_edicion(edicion: EdicionRevisionInput | dict[str, Any]) -> EdicionRevisionInput:
    try:
        datos = EdicionRevisionInput.model_validate(edicion)
    except ValidationError as exc:
        detalle = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise RevisionInvalida(detalle) from exc
    revisor = (datos.revisor or "").strip() or None
    if datos.estado != "pendiente" and revisor is None:
        raise RevisionInvalida("El campo 'revisor' es obligatorio para aprobar o rechazar una revisión.")
    return datos.model_copy(update={"revisor": revisor})


def _leer_base(run_id: str, revision_base: int, db_path: str | Path | None) -> tuple[PaqueteSalida, str, str, set[str], set[str]]:
    """Devuelve la revisión base y el contexto (comunidad, periodo, procesados, aptos) para validar."""
    with _lectura(db_path) as con:
        run = _resolver_run(con, run_id)
        if not run.tiene_paquete:
            raise RevisionNoEncontrada(
                f"La ejecución '{run_id}' no cuenta con un paquete base sobre el cual registrar revisiones."
            )
        filas = con.execute(
            "SELECT source_id, apto_para_publicacion FROM mensajes_analizados WHERE run_id = ?;", (run_id,)
        ).fetchall()
    try:
        base = PaqueteSalida.model_validate(reconstruir_json_paquete_salida(run_id, revision_base, db_path=db_path))
    except ValueError as exc:
        raise RevisionNoEncontrada(f"No existe la revisión {revision_base} para run_id='{run_id}'.") from exc
    procesados = {f["source_id"] for f in filas}
    aptos = {f["source_id"] for f in filas if f["apto_para_publicacion"]}
    return base, run.comunidad or "Desconocido", run.periodo or "Sin_Periodo", procesados, aptos


def _revision_vigente(run_id: str, db_path: str | Path | None) -> int:
    with _lectura(db_path) as con:
        return con.execute(
            "SELECT COALESCE(MAX(numero_revision), 0) FROM paquetes_salida WHERE run_id = ?;", (run_id,)
        ).fetchone()[0]


def registrar_revision(
    run_id: str,
    revision_base: int,
    edicion: EdicionRevisionInput | dict[str, Any],
    db_path: str | Path | None = None,
) -> RespuestaRegistrarRevision:
    datos = _validar_edicion(edicion)
    base, comunidad, periodo, procesados, aptos = _leer_base(run_id, revision_base, db_path)

    # Lo omitido se hereda de la base: el panel solo envía lo que el curador tocó.
    post = datos.post_linkedin or base.activos.post_linkedin
    resumen = datos.resumen_semanal or base.activos.resumen_semanal
    problemas = violaciones_trazabilidad_por_ids(procesados, aptos, post, resumen)
    if problemas:
        raise RevisionInvalida("; ".join(problemas))

    ahora = fecha_utc_ahora()
    try:
        with _transaccion(db_path) as cur:
            actual = cur.execute(
                "SELECT COALESCE(MAX(numero_revision), 0) FROM paquetes_salida WHERE run_id = ?;", (run_id,)
            ).fetchone()[0]
            if actual != revision_base:
                raise ConflictoRevision(revision_base, actual)
            nueva = actual + 1
            ruta = _ruta_objeto(periodo, run_id, nueva)
            paquete = PaqueteSalida(
                schema_version=base.schema_version,
                run_id=run_id,
                request_id=base.request_id,
                fecha_generacion=ahora,
                metadatos_ejecucion=base.metadatos_ejecucion,
                analisis_resumido=base.analisis_resumido,
                activos=base.activos.model_copy(update={"post_linkedin": post, "resumen_semanal": resumen}),
                revision=EstadoRevision(
                    numero_revision=nueva,
                    estado=datos.estado,
                    revisor=datos.revisor,
                    fecha_decision=None if datos.estado == "pendiente" else ahora,
                    comentarios=datos.comentarios,
                ),
                almacenamiento_oci=EvidenciaAlmacenamientoOCI(
                    bucket=base.almacenamiento_oci.bucket,
                    ruta_objeto=ruta,
                    status_almacenamiento="no_iniciado",
                    comprobacion_lectura=False,
                ),
            )
            _insertar_paquete(cur, paquete, comunidad, periodo, sobrescribir=False)
    except ConflictoRevision:
        raise
    except sqlite3.IntegrityError as exc:
        # Otra sesión insertó la misma revisión entre nuestra lectura y la escritura.
        raise ConflictoRevision(revision_base, _revision_vigente(run_id, db_path)) from exc
    except sqlite3.Error as exc:
        logger.error("registrar_revision_fallido", run_id=run_id, error=str(exc)[:300])
        raise EscrituraFallida(f"No se pudo registrar la revisión ({type(exc).__name__}).") from exc

    logger.info("revision_registrada", run_id=run_id, numero_revision=nueva, estado=datos.estado)
    return RespuestaRegistrarRevision(
        run_id=run_id, numero_revision=nueva, estado=datos.estado, ruta_objeto=ruta
    )


def _evidencia(run_id: str, numero_revision: int, db_path: str | Path | None
) -> tuple[sqlite3.Row, sqlite3.Row]:
    with _lectura(db_path) as con:
        _resolver_run(con, run_id)
        fila = con.execute(
            "SELECT bucket, ruta_objeto, status_almacenamiento, comprobacion_lectura "
            "FROM evidencia_almacenamiento_oci WHERE run_id = ? AND numero_revision = ?;",
            (run_id, numero_revision),
        ).fetchone()
        lote = con.execute(
            "SELECT request_id, periodo_referencia FROM lotes_procesamiento WHERE run_id = ?;", (run_id,)
        ).fetchone()
    if fila is None or lote is None:
        raise RevisionNoEncontrada(f"No existe la revisión {numero_revision} para run_id='{run_id}'.")
    return fila, lote


def _subir(cliente: Any, payload: dict[str, Any], lote: sqlite3.Row, run_id: str, n: int) -> Any:
    """El cliente OCI no lanza; si algo inesperado ocurre se traduce a un resultado de error."""
    try:
        return cliente.upload_package(
            package_payload=payload, periodo_referencia=lote["periodo_referencia"], run_id=run_id,
            numero_revision=n, request_id=lote["request_id"],
        )
    except Exception as exc:  # noqa: BLE001 - la UI necesita un estado, no una traza
        logger.error("guardar_oci_excepcion", run_id=run_id, numero_revision=n, tipo=type(exc).__name__)
        return None


def guardar_revision_en_oci(
    run_id: str,
    numero_revision: int,
    cliente: Any | None = None,
    db_path: str | Path | None = None,
) -> RespuestaGuardarOCI:
    fila, lote = _evidencia(run_id, numero_revision, db_path)
    respuesta = {
        "run_id": run_id, "numero_revision": numero_revision, "bucket": fila["bucket"],
        "ruta_objeto": fila["ruta_objeto"],
    }
    if fila["status_almacenamiento"] == "guardado_con_exito" and fila["comprobacion_lectura"]:
        return RespuestaGuardarOCI(
            **respuesta, status_almacenamiento="guardado_con_exito", comprobacion_lectura=True,
            mensaje_error=None, reintento_idempotente=True,
        )

    try:
        payload = reconstruir_json_paquete_salida(run_id, numero_revision, db_path=db_path)
    except ValueError as exc:
        raise RevisionNoEncontrada(f"No existe la revisión {numero_revision} para run_id='{run_id}'.") from exc
    # Siempre se sube la evidencia en su estado inicial: así los bytes son deterministas
    # y un reintento tras un fallo de lectura no choca contra su propio objeto (412).
    payload["almacenamiento_oci"]["status_almacenamiento"] = "no_iniciado"
    payload["almacenamiento_oci"]["comprobacion_lectura"] = False

    cliente = cliente or OCIStorageClient()
    resultado = _subir(cliente, payload, lote, run_id, numero_revision)
    ok = resultado is not None and resultado.status_almacenamiento == "guardado_con_exito" and bool(
        resultado.comprobacion_lectura
    )
    status = "guardado_con_exito" if ok else "guardado_error"
    actualizar_evidencia_oci(run_id, numero_revision, status, ok, db_path=db_path)
    error = None if ok else (
        (resultado.mensaje_error if resultado else None) or "La subida a OCI no pudo verificarse."
    )
    return RespuestaGuardarOCI(
        **{**respuesta, "bucket": resultado.bucket if resultado else fila["bucket"]},
        status_almacenamiento=status, comprobacion_lectura=ok, mensaje_error=error,
        reintento_idempotente=bool(resultado and resultado.reintento_idempotente),
    )
