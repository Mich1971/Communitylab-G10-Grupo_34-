"""Pruebas de data_pipeline/validador.py: adaptador determinista y ids duplicados."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from data_pipeline.validador import (
    _fecha_de_respaldo,
    adaptar_payload_oficial,
    validar_lote_crudo,
)
from esquemas import IngestionLote


def _plano(**extra) -> dict:
    base = {
        "origen_comunidad": "Discord_general",
        "periodo_referencia": "2026-W42",
        "interacciones": [{"autor": "Ana", "canal": "logros", "texto": "Terminé mi proyecto"}],
    }
    base.update(extra)
    return base


def _meta(i: str, fecha: str = "2026-10-12T10:00:00Z") -> dict:
    return {"plataforma": "Discord", "identificador_original": i, "fecha": fecha}


def _lote(items: list[dict]) -> IngestionLote:
    return IngestionLote.model_validate(
        {
            "request_id": "req-v",
            "origen_comunidad": "Discord",
            "periodo_referencia": "2026-W42",
            "interacciones": items,
        }
    )


def _item(i: str, texto: str = "hola", fecha: str = "2026-10-12T10:00:00Z") -> dict:
    return {"id": i, "canal": "c", "texto": texto, "metadata_origen": _meta(i, fecha)}


# --- adaptador ------------------------------------------------------------


def test_adaptador_es_determinista_mismo_payload_mismo_resultado():
    a = adaptar_payload_oficial(_plano())
    b = adaptar_payload_oficial(_plano())
    assert a == b
    assert a["request_id"].startswith("req_")


def test_adaptador_contenido_distinto_da_request_id_distinto():
    otro = _plano(interacciones=[{"canal": "c", "texto": "Otro mensaje"}])
    assert adaptar_payload_oficial(_plano())["request_id"] != adaptar_payload_oficial(otro)["request_id"]


def test_adaptador_respeta_request_id_explicito():
    assert adaptar_payload_oficial(_plano(request_id="jurado-001"))["request_id"] == "jurado-001"


@pytest.mark.parametrize(
    ("periodo", "esperado"),
    [
        ("2026-W42", "2026-10-12T00:00:00+00:00"),  # lunes de la semana ISO 42
        ("2026-10", "2026-10-01T00:00:00+00:00"),
        ("2026-10-18", "2026-10-18T00:00:00+00:00"),
        ("Sin_Periodo", "1970-01-01T00:00:00+00:00"),
        ("2026-W99", "1970-01-01T00:00:00+00:00"),
    ],
)
def test_fecha_de_respaldo_se_deriva_del_periodo_nunca_del_reloj(periodo, esperado):
    assert _fecha_de_respaldo(periodo) == esperado


def test_adaptador_usa_la_fecha_de_respaldo_y_conserva_la_original():
    p = _plano(
        interacciones=[
            {"canal": "c", "texto": "a"},
            {"canal": "c", "texto": "b", "metadata_origen": {"fecha": "2026-10-14T08:00:00Z"}},
        ]
    )
    sal = adaptar_payload_oficial(p)["interacciones"]
    assert sal[0]["metadata_origen"]["fecha"] == "2026-10-12T00:00:00+00:00"
    assert sal[1]["metadata_origen"]["fecha"] == "2026-10-14T08:00:00Z"


def test_adaptador_emite_una_sola_advertencia_agregada_por_lote():
    p = _plano(interacciones=[{"canal": "c", "texto": f"m{i}"} for i in range(500)])
    with capture_logs() as logs:
        adaptar_payload_oficial(p)
    avisos = [e for e in logs if e["event"] == "fechas_origen_fabricadas"]
    assert len(avisos) == 1
    assert avisos[0]["total"] == 500
    assert len(avisos[0]["ejemplo_ids"]) == 3


def test_adaptador_no_advierte_si_todas_las_fechas_vienen_en_el_origen():
    p = _plano(interacciones=[{"canal": "c", "texto": "a", "metadata_origen": {"fecha": "2026-10-12T10:00:00Z"}}])
    with capture_logs() as logs:
        adaptar_payload_oficial(p)
    assert not [e for e in logs if e["event"] == "fechas_origen_fabricadas"]


def test_adaptador_tolera_nulls_explicitos_como_claves_ausentes():
    p = _plano(
        interacciones=[{"autor": None, "canal": None, "tipo": None, "texto": None, "metadata_origen": None}]
    )
    item = adaptar_payload_oficial(p)["interacciones"][0]
    assert (item["autor"], item["canal"], item["tipo"], item["texto"]) == (
        "Anónimo",
        "sin_canal",
        "sin_clasificar",
        "",
    )
    IngestionLote.model_validate(adaptar_payload_oficial(p))  # el sobre es válido


def test_adaptador_convierte_ids_numericos_a_texto_y_genera_los_ausentes():
    p = _plano(interacciones=[{"id": 123, "canal": "c", "texto": "a"}, {"canal": "c", "texto": "b"}])
    sal = adaptar_payload_oficial(p)
    assert sal["interacciones"][0]["id"] == "123"
    assert sal["interacciones"][1]["id"] == f"{sal['request_id']}_msg_002"


def test_adaptador_no_muta_el_payload_de_entrada():
    p = _plano()
    copia = {**p, "interacciones": [dict(i) for i in p["interacciones"]]}
    adaptar_payload_oficial(p)
    assert p == copia


# --- validación registro por registro --------------------------------------


def test_lote_sin_duplicados_pasa_completo():
    validos, rechazados = validar_lote_crudo(_lote([_item("a"), _item("b")]), run_id="run-x")
    assert [v.id for v in validos] == ["a", "b"] and rechazados == []


def test_id_duplicado_se_rechaza_conservando_la_primera_aparicion():
    validos, rechazados = validar_lote_crudo(
        _lote([_item("dup", "primero"), _item("dup", "segundo"), _item("otro")]), run_id="run-x"
    )
    assert [v.id for v in validos] == ["dup", "otro"]
    assert validos[0].texto == "primero"
    assert len(rechazados) == 1
    r = rechazados[0]
    assert r["mensaje_id"] == "dup" and "duplicado" in r["error_detalle"].lower()
    assert r["run_id"] == "run-x" and r["texto_crudo"] == "segundo"


def test_el_motivo_del_duplicado_no_filtra_el_texto_del_mensaje():
    _, rechazados = validar_lote_crudo(_lote([_item("d", "secreto-1"), _item("d", "secreto-2")]))
    assert "secreto" not in rechazados[0]["error_detalle"]


def test_duplicado_de_un_registro_rechazado_no_cuenta_como_duplicado():
    # El primero se rechaza por vacío; el segundo, válido, debe aceptarse.
    validos, rechazados = validar_lote_crudo(_lote([_item("d", "   "), _item("d", "texto válido")]))
    assert [v.id for v in validos] == ["d"]
    assert len(rechazados) == 1 and "vacío" in rechazados[0]["error_detalle"]


def test_texto_vacio_y_fecha_invalida_siguen_rechazandose():
    validos, rechazados = validar_lote_crudo(
        _lote([_item("e", ""), _item("f", "ok", fecha="no-es-fecha"), _item("g")])
    )
    assert [v.id for v in validos] == ["g"]
    assert {r["mensaje_id"] for r in rechazados} == {"e", "f"}


def test_el_diccionario_de_rechazo_conserva_todas_las_llaves_del_contrato_con_la_ui():
    _, rechazados = validar_lote_crudo(_lote([_item("e", "")]), run_id="run-y")
    assert set(rechazados[0]) == {
        "id", "mensaje_id", "error", "error_detalle", "run_id", "request_id",
        "autor", "canal", "texto_crudo", "fecha_aislamiento",
    }


# --- integración validador -> local_db (reconciliación con la versión de Andrea) ----------

import sqlite3

from esquemas import (
    AnalisisCognitivo,
    MensajeAnalizado,
    PuntuacionRelevancia,
    ResumenComunidad,
)
from storage import local_db as L

_PLANO_SUCIO = {
    "origen_comunidad": "Discord",
    "periodo_referencia": "2026-W42",
    "interacciones": [
        {"id": "a", "texto": "uno"},
        {"id": "a", "texto": "mismo id repetido"},
        {"id": "b", "texto": "   "},
        {"texto": "sin id nativo"},
    ],
}


def _analizar(validos, run_id: str, request_id: str) -> AnalisisCognitivo:
    mensajes = [
        MensajeAnalizado(
            source_id=v.id, sentimiento="Positivo", categoria_enrutamiento="Logro", regla_aplicada="r",
            temas=["python"], entidades_relevantes=[],
            puntuacion_relevancia=PuntuacionRelevancia(evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2),
            motivo_seleccion="motivo suficiente largo", apto_para_publicacion=True,
        )
        for v in validos
    ]
    n = len(validos)
    return AnalisisCognitivo(
        run_id=run_id, request_id=request_id,
        resumen_comunidad=ResumenComunidad(
            total_interacciones_procesadas=n, registros_validos=n, sentimiento_predominante="Positivo",
            distribucion_sentimiento={"Positivo": n}, temas_principales=["python"]),
        mensajes_analizados=mensajes)


def _persistir(db, run_id: str, plano: dict) -> tuple[IngestionLote, list, list, L.ResultadoPersistencia]:
    lote = IngestionLote.model_validate(adaptar_payload_oficial(plano))
    validos, rechazados = validar_lote_crudo(lote, run_id=run_id)
    res = L.persistir_resultado(
        run_id=run_id, request_id=lote.request_id, origen_comunidad="Discord",
        periodo_referencia=lote.periodo_referencia, total_recibidos=len(lote.interacciones),
        total_validos=len(validos), rechazados_validador=rechazados,
        analisis_cognitivo=_analizar(validos, run_id, lote.request_id),
        interacciones_origen=validos, db_path=db)
    return lote, validos, rechazados, res


def test_flujo_plano_adaptar_validar_persistir_cuadra_los_conteos(tmp_path):
    db = tmp_path / "c.db"
    L.inicializar_db(db)
    _, validos, rechazados, res = _persistir(db, "run-1", _PLANO_SUCIO)
    # Con id repetido y texto vacío: 2 válidos y 2 rechazados; lo que se reporta es lo que hay en la base.
    assert validos[0].id == "a" and len(validos) == 2 and len(rechazados) == 2
    assert res.mensajes_sanos == 2 and res.rechazados_validador == 2
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM mensajes_analizados").fetchone()[0] == 2
    assert con.execute("SELECT total_recibidos, total_validos, total_rechazados FROM lotes_procesamiento").fetchone() == (4, 2, 2)
    assert con.execute("SELECT COUNT(*) FROM auditoria_rechazados WHERE origen='validador'").fetchone()[0] == 2
    con.close()


def test_un_id_repetido_sin_el_rechazo_del_validador_haria_mentir_a_los_conteos(tmp_path):
    """Por qué el rechazo de duplicados es obligatorio: sin él, 3 'sanos' se reportan pero la base guarda 2."""
    db = tmp_path / "c.db"
    L.inicializar_db(db)
    lote = IngestionLote.model_validate(adaptar_payload_oficial(_PLANO_SUCIO))
    sin_filtro = [
        v for v in validar_lote_crudo(lote, run_id="r")[0]
    ]
    duplicado = sin_filtro[0].model_copy()  # simula un validador que no rechaza ids repetidos
    con_dup = [sin_filtro[0], duplicado, sin_filtro[1]]
    res = L.persistir_resultado(
        run_id="r", request_id=lote.request_id, origen_comunidad="Discord", periodo_referencia="2026-W42",
        total_recibidos=3, total_validos=3, analisis_cognitivo=_analizar(con_dup, "r", lote.request_id),
        interacciones_origen=con_dup, db_path=db)
    con = sqlite3.connect(db)
    guardados = con.execute("SELECT COUNT(*) FROM mensajes_analizados").fetchone()[0]
    con.close()
    assert res.mensajes_sanos == 3 and guardados == 2


def test_reintentar_el_mismo_payload_plano_es_idempotente_de_extremo_a_extremo(tmp_path):
    db = tmp_path / "c.db"
    L.inicializar_db(db)
    l1, *_ = _persistir(db, "run-1", _PLANO_SUCIO)
    l2, *_ = _persistir(db, "run-1", _PLANO_SUCIO)
    assert l1.request_id == l2.request_id  # misma huella -> la API respondería 200, no 409
    con = sqlite3.connect(db)
    assert [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("lotes_procesamiento", "mensajes_analizados", "auditoria_rechazados", "mensaje_temas")] == [1, 2, 2, 2]
    con.close()


def test_los_rechazos_del_validador_son_insertables_tal_cual_en_auditoria(tmp_path):
    """El diccionario de rechazo trae todas las llaves que espera `registrar_rechazados_validador`."""
    db = tmp_path / "c.db"
    L.inicializar_db(db)
    lote = IngestionLote.model_validate(adaptar_payload_oficial(_PLANO_SUCIO))
    _, rechazados = validar_lote_crudo(lote, run_id="run-r")
    assert L.registrar_rechazados_validador(rechazados, db_path=db) == 2
    assert L.registrar_rechazados_validador(rechazados, db_path=db) == 2  # reintento: no duplica
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM auditoria_rechazados").fetchone()[0] == 2
    con.close()


def test_validar_sin_run_id_se_puede_persistir_indicando_run_id_al_guardar(tmp_path):
    db = tmp_path / "c.db"
    L.inicializar_db(db)
    lote = IngestionLote.model_validate(adaptar_payload_oficial(_PLANO_SUCIO))
    _, rechazados = validar_lote_crudo(lote)  # run_id opcional -> cada rechazo trae run_id=None
    assert all(r["run_id"] is None for r in rechazados)
    with pytest.raises(ValueError, match="run_id"):
        L.registrar_rechazados_validador(rechazados, db_path=db)
    assert L.registrar_rechazados_validador(rechazados, db_path=db, run_id="run-z") == 2
