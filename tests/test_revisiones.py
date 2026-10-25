"""Pruebas de las escrituras de curaduría: revisiones inmutables y guardado verificado en OCI."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest
from test_consultas import (  # noqa: F401 - `db` es un fixture reutilizado
    PERIODO,
    _sembrar,
    db,
)

from storage import consultas as c
from storage import revisiones as r

RUN = "run-rev"


@pytest.fixture
def run(db):  # noqa: F811
    _sembrar(db, RUN)
    return db


def _revisiones(ruta_db: str) -> list[int]:
    with sqlite3.connect(ruta_db) as con:
        return [f[0] for f in con.execute(
            "SELECT numero_revision FROM paquetes_salida WHERE run_id = ? ORDER BY 1;", (RUN,))]


def test_aprobar_sin_editar_crea_revision_2_y_no_toca_la_1(run):
    antes = c.obtener_revision(RUN, 1, db_path=run)
    resp = r.registrar_revision(RUN, 1, {"estado": "aprobado", "revisor": " ana "}, db_path=run)
    assert (resp.numero_revision, resp.estado) == (2, "aprobado")
    assert resp.ruta_objeto == f"activos/{PERIODO}/{RUN}/revision-002.json"
    assert c.obtener_revision(RUN, 1, db_path=run) == antes
    nueva = c.obtener_revision(RUN, 2, db_path=run)
    assert nueva.paquete.revision.revisor == "ana"
    assert nueva.paquete.revision.fecha_decision is not None
    assert nueva.paquete.activos.post_linkedin == antes.paquete.activos.post_linkedin
    assert nueva.evidencia_oci.status_almacenamiento == "no_iniciado"
    assert nueva.evidencia_oci.ruta_objeto == resp.ruta_objeto


def test_edicion_pendiente_sin_revisor_y_vigente(run):
    r.registrar_revision(RUN, 1, {
        "estado": "pendiente",
        "post_linkedin": {"titulo": "Titulo nuevo", "cuerpo": "Cuerpo editado por el curador humano",
                          "hashtags": [], "source_ids": ["msg_001"]},
    }, db_path=run)
    listado = c.listar_revisiones(RUN, db_path=run)
    assert listado.revision_vigente == 2
    paquete = c.obtener_revision(RUN, 2, db_path=run).paquete
    assert paquete.activos.post_linkedin.titulo == "Titulo nuevo"
    assert paquete.revision.fecha_decision is None
    # El resumen omitido se hereda de la base.
    assert paquete.activos.resumen_semanal.source_ids == ["msg_001", "msg_002"]


def test_aprobar_o_rechazar_exige_revisor(run):
    for estado in ("aprobado", "rechazado"):
        with pytest.raises(c.RevisionInvalida):
            r.registrar_revision(RUN, 1, {"estado": estado, "revisor": "  "}, db_path=run)
    assert _revisiones(run) == [1]


def test_post_no_puede_citar_mensaje_no_apto_pero_el_resumen_si(run):
    post = {"titulo": "Titulo nuevo", "cuerpo": "Cuerpo editado por el curador humano", "source_ids": ["msg_002"]}
    with pytest.raises(c.RevisionInvalida) as exc:
        r.registrar_revision(RUN, 1, {"estado": "pendiente", "post_linkedin": post}, db_path=run)
    assert "post_linkedin.source_ids fuera del conjunto de mensajes aptos: ['msg_002']" in exc.value.detalle
    resumen = {"seccion": "Actividad", "titular": "Titular nuevo", "resumen": "Resumen editado suficientemente largo",
               "source_ids": ["msg_002"]}
    assert r.registrar_revision(RUN, 1, {"estado": "pendiente", "resumen_semanal": resumen}, db_path=run).numero_revision == 2


def test_source_id_inexistente_y_cuerpo_corto_son_invalidos(run):
    inexistente = {"titulo": "Titulo nuevo", "cuerpo": "Cuerpo editado por el curador humano", "source_ids": ["msg_999"]}
    corto = {"titulo": "Titulo nuevo", "cuerpo": "corto", "source_ids": ["msg_001"]}
    for post in (inexistente, corto):
        with pytest.raises(c.RevisionInvalida) as exc:
            r.registrar_revision(RUN, 1, {"estado": "pendiente", "post_linkedin": post}, db_path=run)
        assert exc.value.http_status == 422
    assert _revisiones(run) == [1]


def test_conflicto_de_revision_base_no_escribe(run):
    r.registrar_revision(RUN, 1, {"estado": "aprobado", "revisor": "ana"}, db_path=run)
    with pytest.raises(c.ConflictoRevision) as exc:
        r.registrar_revision(RUN, 1, {"estado": "rechazado", "revisor": "luis"}, db_path=run)
    assert (exc.value.http_status, exc.value.revision_actual) == (409, 2)
    assert _revisiones(run) == [1, 2]


def test_base_inexistente_run_sin_paquete_y_run_inexistente(db):  # noqa: F811
    _sembrar(db, "run-ok")
    _sembrar(db, "run-sin", con_paquete=False)
    with pytest.raises(c.RevisionNoEncontrada, match="No existe la revisión 7"):
        r.registrar_revision("run-ok", 7, {"estado": "aprobado", "revisor": "a"}, db_path=db)
    with pytest.raises(c.RevisionNoEncontrada, match="no cuenta con un paquete base"):
        r.registrar_revision("run-sin", 1, {"estado": "aprobado", "revisor": "a"}, db_path=db)
    with pytest.raises(c.EjecucionNoEncontrada):
        r.registrar_revision("run-x", 1, {"estado": "aprobado", "revisor": "a"}, db_path=db)


def test_dos_hilos_con_la_misma_base_solo_uno_gana(run):
    resultados: list[object] = []

    def guardar(revisor: str) -> None:
        try:
            resultados.append(r.registrar_revision(RUN, 1, {"estado": "aprobado", "revisor": revisor}, db_path=run))
        except c.ConflictoRevision as exc:
            resultados.append(exc)

    hilos = [threading.Thread(target=guardar, args=(n,)) for n in ("ana", "luis")]
    [h.start() for h in hilos]
    [h.join() for h in hilos]
    assert sorted(type(x).__name__ for x in resultados) == ["ConflictoRevision", "RespuestaRegistrarRevision"]
    assert _revisiones(run) == [1, 2]


# ---------------------------------------------------------------- OCI
class _ClienteFalso:
    def __init__(self, *resultados: object) -> None:
        self.resultados = list(resultados)
        self.llamadas: list[dict] = []

    def upload_package(self, **kwargs):
        self.llamadas.append(kwargs)
        res = self.resultados.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


def _resultado(ok: bool = True, **extra) -> SimpleNamespace:
    base = {
        "bucket": "bucket-x", "ruta_objeto": "ruta", "mensaje_error": None if ok else "Fallo de red",
        "status_almacenamiento": "guardado_con_exito" if ok else "guardado_error",
        "comprobacion_lectura": ok, "reintento_idempotente": False,
    }
    return SimpleNamespace(**{**base, **extra})


def _evidencia(ruta_db: str, n: int):
    ev = c.obtener_revision(RUN, n, db_path=ruta_db).evidencia_oci
    return ev.status_almacenamiento, ev.comprobacion_lectura


def test_guardar_en_oci_exitoso_y_segundo_llamado_es_idempotente(run):
    cliente = _ClienteFalso(_resultado())
    resp = r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run)
    assert (resp.status_almacenamiento, resp.comprobacion_lectura, resp.mensaje_error) == ("guardado_con_exito", True, None)
    assert _evidencia(run, 1) == ("guardado_con_exito", True)
    llamada = cliente.llamadas[0]
    assert llamada["numero_revision"] == 1 and llamada["periodo_referencia"] == PERIODO
    assert llamada["package_payload"]["almacenamiento_oci"]["status_almacenamiento"] == "no_iniciado"
    again = r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run)
    assert again.reintento_idempotente is True and len(cliente.llamadas) == 1


def test_guardar_en_oci_fallido_se_registra_y_se_puede_reintentar(run):
    cliente = _ClienteFalso(_resultado(ok=False), RuntimeError("boom"), _resultado())
    fallo = r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run)
    assert (fallo.status_almacenamiento, fallo.comprobacion_lectura) == ("guardado_error", False)
    assert fallo.mensaje_error == "Fallo de red" and _evidencia(run, 1) == ("guardado_error", False)
    excepcion = r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run)
    assert excepcion.status_almacenamiento == "guardado_error" and excepcion.mensaje_error
    assert r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run).status_almacenamiento == "guardado_con_exito"
    assert len(cliente.llamadas) == 3


def test_estado_exito_requiere_lectura_verificada(run):
    cliente = _ClienteFalso(_resultado(comprobacion_lectura=False))
    resp = r.guardar_revision_en_oci(RUN, 1, cliente=cliente, db_path=run)
    assert (resp.status_almacenamiento, resp.comprobacion_lectura) == ("guardado_error", False)


def test_guardar_revision_nueva_usa_su_numero_y_404(run):
    r.registrar_revision(RUN, 1, {"estado": "aprobado", "revisor": "ana"}, db_path=run)
    cliente = _ClienteFalso(_resultado())
    r.guardar_revision_en_oci(RUN, 2, cliente=cliente, db_path=run)
    assert cliente.llamadas[0]["numero_revision"] == 2
    assert _evidencia(run, 1) == ("no_iniciado", False)
    with pytest.raises(c.RevisionNoEncontrada):
        r.guardar_revision_en_oci(RUN, 9, cliente=cliente, db_path=run)
    with pytest.raises(c.EjecucionNoEncontrada):
        r.guardar_revision_en_oci("run-x", 1, cliente=cliente, db_path=run)
