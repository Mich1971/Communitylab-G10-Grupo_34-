"""Pruebas de las consultas de lectura (operaciones 1 a 8) con SQLite real."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.registro import RegistroIdempotencia
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EstadoRevision,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MensajeAnalizado,
    MetadataOrigenValidado,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)
from storage import consultas as c
from storage.local_db import LocalDatabaseManager, persistir_resultado

PERIODO = "Semana_04"
COMUNIDAD = "Discord_G10"


@pytest.fixture
def db(tmp_path: Path) -> str:
    ruta = str(tmp_path / "communitylab.db")
    LocalDatabaseManager(ruta)
    RegistroIdempotencia(Path(ruta)).inicializar()
    return ruta


def _solicitud(db: str, run_id: str, estado: str, recibido: str = "2026-10-24T18:00:00.000+00:00") -> None:
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO solicitudes_ingesta (request_id, hash_sha256, run_id, estado, recibido_en, actualizado_en) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (f"req-{run_id}", "a" * 64, run_id, estado, recibido, recibido),
        )


def _interaccion(i: str, texto: str) -> InteraccionValidada:
    return InteraccionValidada(
        id=i, autor=f"autor_{i}", canal="#general", tipo="testimonio", texto=texto,
        metadata_origen=MetadataOrigenValidado(
            plataforma=COMUNIDAD, identificador_original=i, fecha=datetime(2026, 10, 24, tzinfo=UTC)
        ),
    )


def _mensaje(i: str, categoria: str, apto: bool, soporte: bool = False) -> MensajeAnalizado:
    return MensajeAnalizado(
        source_id=i, sentimiento="Positivo", categoria_enrutamiento=categoria,
        regla_aplicada="Regla de prueba documentada", temas=["OCI", "empleo"],
        entidades_relevantes=["OCI"],
        puntuacion_relevancia=PuntuacionRelevancia(evidencia_explicita=2, utilidad_comunitaria=1, claridad_contexto=2),
        motivo_seleccion="Motivo suficientemente largo para validar.", requiere_soporte=soporte,
        apto_para_publicacion=apto,
    )


def _sembrar(db: str, run_id: str, *, con_analisis: bool = True, con_paquete: bool = True,
             estado: str = "completado", rechazados: int = 1) -> None:
    """Persiste un run con dos mensajes (msg_001 apto, msg_002 no apto) y `rechazados` rechazos."""
    _solicitud(db, run_id, estado)
    request_id = f"req-{run_id}"
    interacciones = [_interaccion("msg_001", "Texto original uno"), _interaccion("msg_002", "Texto original dos")]
    resumen = ResumenComunidad(
        total_interacciones_procesadas=2, registros_validos=2, registros_rechazados=rechazados,
        sentimiento_predominante="Positivo", distribucion_sentimiento={"Positivo": 2},
        temas_principales=["OCI"], alertas_soporte=[],
    )
    analisis = AnalisisCognitivo(
        run_id=run_id, request_id=request_id, resumen_comunidad=resumen,
        mensajes_analizados=[_mensaje("msg_001", "Logro", True), _mensaje("msg_002", "Duda", False, True)],
    )
    paquete = PaqueteSalida(
        run_id=run_id, request_id=request_id,
        metadatos_ejecucion=MetadatosEjecucion(modelo="m", prompt_version="v1", latencia_ms=10, tokens_totales=5),
        analisis_resumido=resumen,
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(titulo="Titulo post", cuerpo="Cuerpo del post de LinkedIn largo",
                                       hashtags=["#OCI"], source_ids=["msg_001"]),
            resumen_semanal=ResumenSemanal(seccion="Actividad", titular="Titular semanal",
                                           resumen="Resumen semanal suficientemente largo", source_ids=["msg_001", "msg_002"]),
            casos_no_publicables=[CasoNoPublicable.desde_mensaje_analizado(_mensaje("msg_002", "Duda", False, True))],
        ),
        revision=EstadoRevision(),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(ruta_objeto=f"activos/{PERIODO}/{run_id}/revision-001.json"),
    )
    persistir_resultado(
        run_id=run_id, request_id=request_id, origen_comunidad=COMUNIDAD, periodo_referencia=PERIODO,
        total_recibidos=2 + rechazados, total_validos=2,
        rechazados_validador=[{"mensaje_id": f"bad_{k}", "autor": "x", "canal": "#c",
                               "error_detalle": "El texto está vacío", "texto_crudo": "secreto"}
                              for k in range(rechazados)],
        analisis_cognitivo=analisis if con_analisis else None,
        interacciones_origen=interacciones if con_analisis else (),
        paquete=paquete if con_paquete else None, db_path=db,
    )
    with sqlite3.connect(db) as con:
        con.execute("UPDATE lotes_procesamiento SET estado_flujo = ? WHERE run_id = ?;", (estado, run_id))


# --------------------------------------------------------------------------- estados
def test_ejecucion_inexistente_es_404_en_todas_las_operaciones(db):
    llamadas = [
        lambda: c.obtener_ejecucion("run-x", db_path=db),
        lambda: c.listar_mensajes("run-x", db_path=db),
        lambda: c.obtener_fuentes("run-x", ["a"], db_path=db),
        lambda: c.obtener_resumen("run-x", db_path=db),
        lambda: c.listar_rechazados("run-x", db_path=db),
        lambda: c.listar_revisiones("run-x", db_path=db),
        lambda: c.obtener_revision("run-x", 1, db_path=db),
    ]
    for llamada in llamadas:
        with pytest.raises(c.EjecucionNoEncontrada) as exc:
            llamada()
        assert exc.value.http_status == 404 and exc.value.codigo == "ejecucion_no_encontrada"


def test_en_curso_no_disponible_con_motivo(db):
    _solicitud(db, "run-cola", "en_cola")
    det = c.obtener_ejecucion("run-cola", db_path=db)
    assert (det.categoria_estado, det.tiene_paquete, det.comunidad) == ("en_curso", False, None)
    for resp in (c.listar_mensajes("run-cola", db_path=db), c.obtener_resumen("run-cola", db_path=db),
                 c.listar_revisiones("run-cola", db_path=db), c.listar_rechazados("run-cola", db_path=db)):
        assert (resp.disponible, resp.motivo, resp.categoria_estado) == (False, "en_curso", "en_curso")


def test_completado_con_paquete(db):
    _sembrar(db, "run-ok")
    det = c.obtener_ejecucion("run-ok", db_path=db)
    assert (det.categoria_estado, det.tiene_paquete, det.revision_vigente) == ("finalizada_con_paquete", True, 1)
    assert (det.total_recibidos, det.total_validos, det.total_rechazados) == (3, 2, 1)
    assert det.advertencia is None
    msgs = c.listar_mensajes("run-ok", db_path=db)
    assert msgs.disponible and msgs.total == 2 and msgs.siguiente_cursor is None
    primero = msgs.items[0]
    assert primero.source_id == "msg_001" and primero.puntuacion.total == 5
    assert primero.temas == ["OCI", "empleo"] and primero.entidades == ["OCI"]
    res = c.obtener_resumen("run-ok", db_path=db)
    assert res.resumen.distribucion_sentimiento == {"Positivo": 2} and res.resumen.temas_principales == ["OCI"]
    revs = c.listar_revisiones("run-ok", db_path=db)
    assert revs.disponible and revs.revision_vigente == 1
    assert (revs.items[0].estado, revs.items[0].status_almacenamiento) == ("pendiente", "no_iniciado")


def test_sin_contenido_publicable_tiene_analisis_pero_no_revisiones(db):
    _sembrar(db, "run-sc", con_paquete=False, estado="sin_contenido_publicable")
    assert c.listar_mensajes("run-sc", db_path=db).disponible is True
    assert c.obtener_resumen("run-sc", db_path=db).disponible is True
    revs = c.listar_revisiones("run-sc", db_path=db)
    assert (revs.disponible, revs.motivo, revs.items) == (False, "sin_paquete", [])
    assert c.obtener_ejecucion("run-sc", db_path=db).categoria_estado == "finalizada_sin_paquete"


def test_sin_registros_validos_solo_tiene_rechazados(db):
    _sembrar(db, "run-sr", con_analisis=False, con_paquete=False, estado="sin_registros_validos")
    msgs = c.listar_mensajes("run-sr", db_path=db)
    assert (msgs.disponible, msgs.motivo) == (False, "sin_analisis")
    assert c.obtener_resumen("run-sr", db_path=db).motivo == "sin_analisis"
    rech = c.listar_rechazados("run-sr", db_path=db)
    assert rech.disponible and rech.total == 1


def test_fallido_con_datos_persistidos_se_marca_fallida(db):
    _sembrar(db, "run-f", con_paquete=False, estado="fallido")
    msgs = c.listar_mensajes("run-f", db_path=db)
    assert (msgs.categoria_estado, msgs.disponible) == ("fallida", True)


def test_completado_sin_paquete_advierte(db):
    _sembrar(db, "run-raro", con_paquete=False, estado="completado")
    det = c.obtener_ejecucion("run-raro", db_path=db)
    assert det.tiene_paquete is False and det.advertencia


# --------------------------------------------------------------------------- mensajes y fuentes
def test_listar_mensajes_filtros_y_paginacion(db):
    _sembrar(db, "run-ok")
    assert [m.source_id for m in c.listar_mensajes("run-ok", apto=True, db_path=db).items] == ["msg_001"]
    assert [m.source_id for m in c.listar_mensajes("run-ok", requiere_soporte=True, db_path=db).items] == ["msg_002"]
    assert c.listar_mensajes("run-ok", categoria="Duda", db_path=db).total == 1
    p1 = c.listar_mensajes("run-ok", limite=1, db_path=db)
    assert p1.siguiente_cursor and p1.items[0].source_id == "msg_001" and p1.total == 2
    p2 = c.listar_mensajes("run-ok", limite=1, cursor=p1.siguiente_cursor, db_path=db)
    assert p2.items[0].source_id == "msg_002" and p2.siguiente_cursor is None


@pytest.mark.parametrize("kwargs", [{"limite": 0}, {"limite": 201}, {"categoria": "Otra"},
                                    {"sentimiento": "Feliz"}, {"cursor": "no-es-un-cursor"}])
def test_listar_mensajes_parametros_invalidos(db, kwargs):
    _sembrar(db, "run-ok")
    with pytest.raises(c.ParametroInvalido):
        c.listar_mensajes("run-ok", db_path=db, **kwargs)


def test_obtener_fuentes_texto_original_y_no_encontrados(db):
    _sembrar(db, "run-ok")
    resp = c.obtener_fuentes("run-ok", ["msg_002", "msg_999", "msg_002"], db_path=db)
    assert [f.source_id for f in resp.items] == ["msg_002"]
    assert resp.items[0].texto == "Texto original dos" and resp.items[0].tipo_texto == "original_validado"
    assert resp.no_encontrados == ["msg_999"]


@pytest.mark.parametrize("ids", [[], [""], [f"m{i}" for i in range(51)]])
def test_obtener_fuentes_cantidad_invalida(db, ids):
    _sembrar(db, "run-ok")
    with pytest.raises(c.ParametroInvalido):
        c.obtener_fuentes("run-ok", ids, db_path=db)


# --------------------------------------------------------------------------- rechazados
def test_rechazados_oculta_texto_salvo_que_se_pida(db):
    _sembrar(db, "run-ok")
    assert c.listar_rechazados("run-ok", db_path=db).items[0].texto_crudo is None
    assert c.listar_rechazados("run-ok", incluir_texto=True, db_path=db).items[0].texto_crudo == "secreto"
    assert c.listar_rechazados("run-ok", origen="analisis", db_path=db).items == []
    with pytest.raises(c.ParametroInvalido):
        c.listar_rechazados("run-ok", origen="otro", db_path=db)


# --------------------------------------------------------------------------- revisiones
def test_obtener_revision_devuelve_contrato_3_y_evidencia(db):
    _sembrar(db, "run-ok")
    resp = c.obtener_revision("run-ok", 1, db_path=db)
    assert resp.paquete.activos.post_linkedin.source_ids == ["msg_001"]
    assert resp.evidencia_oci.ruta_objeto == f"activos/{PERIODO}/run-ok/revision-001.json"
    assert [x.source_id for x in resp.paquete.activos.casos_no_publicables] == ["msg_002"]
    with pytest.raises(c.RevisionNoEncontrada):
        c.obtener_revision("run-ok", 9, db_path=db)


# --------------------------------------------------------------------------- histórico
def _lote_masivo(db: str, n: int) -> None:
    with sqlite3.connect(db) as con:
        for i in range(n):
            rid, fecha = f"run-{i:04d}", f"2026-10-24T10:{i // 60:02d}:{i % 60:02d}.000+00:00"
            con.execute(
                "INSERT INTO solicitudes_ingesta (request_id, hash_sha256, run_id, estado, recibido_en, actualizado_en) "
                "VALUES (?, ?, ?, ?, ?, ?);", (f"r{i}", "a" * 64, rid, "completado" if i % 2 else "en_cola", fecha, fecha))
            if i % 2:
                con.execute(
                    "INSERT INTO lotes_procesamiento (run_id, request_id, origen_comunidad, periodo_referencia, "
                    "total_recibidos, total_validos, total_rechazados, fecha_procesamiento) VALUES (?,?,?,?,1,1,0,?);",
                    (rid, f"r{i}", COMUNIDAD if i % 4 == 1 else "Slack", PERIODO if i % 3 else "Semana_05", fecha))


def _recorrer(db: str, **filtros) -> list[str]:
    ids, cursor = [], None
    while True:
        pag = c.listar_ejecuciones(limite=25, cursor=cursor, db_path=db, **filtros)
        ids += [i.run_id for i in pag.items]
        cursor = pag.siguiente_cursor
        if cursor is None:
            return ids


def test_historico_pagina_sin_repetir_ni_omitir_y_filtra_como_sql(db):
    _lote_masivo(db, 120)
    ids = _recorrer(db)
    assert len(ids) == len(set(ids)) == 120 and ids == sorted(ids, reverse=True)
    assert c.listar_ejecuciones(db_path=db).total == 120
    with sqlite3.connect(db) as con:
        esperado = con.execute(
            "SELECT COUNT(*) FROM solicitudes_ingesta s JOIN lotes_procesamiento l USING (run_id) "
            "WHERE s.estado = 'completado' AND l.periodo_referencia = ? AND l.origen_comunidad = ?;",
            ("Semana_05", COMUNIDAD)).fetchone()[0]
    filtrado = c.listar_ejecuciones(limite=100, estado="completado", periodo="Semana_05", comunidad=COMUNIDAD, db_path=db)
    assert filtrado.total == esperado == len(filtrado.items)


def test_historico_estable_si_entran_lotes_durante_la_paginacion(db):
    _lote_masivo(db, 60)
    p1 = c.listar_ejecuciones(limite=25, db_path=db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO solicitudes_ingesta (request_id, hash_sha256, run_id, estado, recibido_en, actualizado_en) "
            "VALUES ('nuevo', ?, 'run-9999', 'en_cola', '2026-10-24T23:00:00.000+00:00', '2026-10-24T23:00:00.000+00:00');",
            ("a" * 64,))
    resto, cursor = [], p1.siguiente_cursor
    while cursor:
        pag = c.listar_ejecuciones(limite=25, cursor=cursor, db_path=db)
        resto += [i.run_id for i in pag.items]
        cursor = pag.siguiente_cursor
    vistos = [i.run_id for i in p1.items] + resto
    assert len(vistos) == len(set(vistos)) == 60 and "run-9999" not in vistos


def test_historico_incluye_ejecuciones_manuales_sin_fecha_de_recepcion(db):
    from storage.local_db import registrar_lote_procesamiento

    registrar_lote_procesamiento("run-manual", "req-m", COMUNIDAD, PERIODO, 1, 1, 0, db_path=db)
    item = next(i for i in c.listar_ejecuciones(db_path=db).items if i.run_id == "run-manual")
    assert item.fecha_recepcion is None and item.comunidad == COMUNIDAD
    assert c.obtener_ejecucion("run-manual", db_path=db).run_id == "run-manual"


def test_historico_parametros_invalidos(db):
    for kwargs in ({"limite": 500}, {"estado": "raro"}, {"cursor": "!!"}):
        with pytest.raises(c.ParametroInvalido):
            c.listar_ejecuciones(db_path=db, **kwargs)


# --------------------------------------------------------------------------- fallos de lectura
def test_base_dañada_devuelve_lectura_fallida_nunca_lista_vacia(tmp_path):
    rota = tmp_path / "rota.db"
    rota.write_bytes(b"esto no es una base sqlite" * 50)
    for llamada in (
        lambda: c.listar_ejecuciones(db_path=rota),
        lambda: c.obtener_ejecucion("run-x", db_path=rota),
        lambda: c.listar_mensajes("run-x", db_path=rota),
        lambda: c.obtener_fuentes("run-x", ["a"], db_path=rota),
        lambda: c.obtener_resumen("run-x", db_path=rota),
        lambda: c.listar_rechazados("run-x", db_path=rota),
        lambda: c.listar_revisiones("run-x", db_path=rota),
        lambda: c.obtener_revision("run-x", 1, db_path=rota),
    ):
        with pytest.raises(c.LecturaFallida) as exc:
            llamada()
        assert exc.value.http_status == 503


def test_la_conexion_de_lectura_no_puede_escribir(db):
    with c._lectura(db) as con, pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM lotes_procesamiento;")
