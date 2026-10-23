"""Pruebas de storage/local_db.py (persistencia SQLite del MVP).

Cada prueba usa una base temporal. Cubren la clave compuesta (run_id, source_id),
la atomicidad de `persistir_resultado`, la idempotencia ante reejecuciones del worker,
el viaje de ida y vuelta del Contrato 3 y la coexistencia con el registro de la API.
"""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from api.registro import RegistroIdempotencia
from data_pipeline.validador import validar_lote_crudo
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EstadoRevision,
    EvidenciaAlmacenamientoOCI,
    IngestionLote,
    MensajeAnalizado,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)
from storage import local_db as L

# --- constructores -----------------------------------------------------------


def _lote(rid: str, ids: list[str]) -> IngestionLote:
    return IngestionLote.model_validate(
        {
            "request_id": rid,
            "origen_comunidad": "Discord",
            "periodo_referencia": "2026-W42",
            "interacciones": [
                {
                    "id": i, "canal": "c", "texto": f"texto {i}",
                    "metadata_origen": {"plataforma": "Discord", "identificador_original": i,
                                        "fecha": "2026-10-12T10:00:00Z"},
                }
                for i in ids
            ],
        }
    )


def _msg(i: str, apto: bool = True) -> MensajeAnalizado:
    return MensajeAnalizado(
        source_id=i, sentimiento="Positivo", categoria_enrutamiento="Logro", regla_aplicada="r",
        temas=["python"], entidades_relevantes=["x"],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2),
        motivo_seleccion="motivo suficiente largo", apto_para_publicacion=apto,
    )


def _resumen(n: int) -> ResumenComunidad:
    return ResumenComunidad(
        total_interacciones_procesadas=n, registros_validos=n, sentimiento_predominante="Positivo",
        distribucion_sentimiento={"Positivo": n}, temas_principales=["python"],
        alertas_soporte=["alerta uno"])


def _analisis(run: str, req: str, ids: list[str], no_aptos: tuple[str, ...] = ()) -> AnalisisCognitivo:
    return AnalisisCognitivo(
        run_id=run, request_id=req, resumen_comunidad=_resumen(len(ids)),
        mensajes_analizados=[_msg(i, apto=i not in no_aptos) for i in ids])


def _paquete(run: str, req: str, ids: list[str], no_aptos: tuple[str, ...] = (), rev: int = 1,
             citados: list[str] | None = None, status: str = "no_iniciado") -> PaqueteSalida:
    aptos = citados or [i for i in ids if i not in no_aptos]
    casos = [CasoNoPublicable.desde_mensaje_analizado(_msg(i, apto=False)) for i in sorted(no_aptos)]
    return PaqueteSalida(
        run_id=run, request_id=req,
        metadatos_ejecucion=MetadatosEjecucion(modelo="gpt-x", prompt_version="1.0.0+abc", latencia_ms=120, tokens_totales=345),
        analisis_resumido=_resumen(len(ids)),
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 30, hashtags=["#a", "#b"], source_ids=aptos[:2]),
            resumen_semanal=ResumenSemanal(seccion="Logros", titular="Titular ok", resumen="r" * 30, source_ids=aptos[:1]),
            casos_no_publicables=casos),
        revision=EstadoRevision(numero_revision=rev),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(ruta_objeto=f"activos/2026-W42/{run}/revision-{rev}.json", status_almacenamiento=status),
    )


def _telemetria(**kw) -> SimpleNamespace:
    base = {"proveedor": "openai", "modelo": "gpt-x", "latencia_ms": 100, "tokens_entrada": 50,
            "tokens_salida": 30, "tokens_totales": 80, "tokens_cacheados": 10}
    return SimpleNamespace(**{**base, **kw})


def _todo(run: str, req: str, ids: list[str], no_aptos: tuple[str, ...] = (), **extra) -> dict:
    validos, rechazados = validar_lote_crudo(_lote(req, ids), run_id=run)
    return {
        "run_id": run, "request_id": req, "origen_comunidad": "Discord", "periodo_referencia": "2026-W42",
        "total_recibidos": len(ids), "total_validos": len(validos), "rechazados_validador": rechazados,
        "analisis_cognitivo": _analisis(run, req, ids, no_aptos), "interacciones_origen": validos,
        "paquete": None if set(ids) <= set(no_aptos) else _paquete(run, req, ids, no_aptos),
        "telemetria": [_telemetria()], **extra,
    }


@pytest.fixture
def db(tmp_path):
    ruta = tmp_path / "data" / "c.db"
    L.inicializar_db(ruta)
    return ruta


def _n(db, tabla: str, donde: str = "1=1") -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {tabla} WHERE {donde}").fetchone()[0]
    finally:
        con.close()


TABLAS_DEL_GESTOR = 19


# --- esquema, ruta y coexistencia ---------------------------------------------


def test_inicializar_es_idempotente_y_crea_las_19_tablas(db):
    L.inicializar_db(db)
    L.inicializar_db(db)
    con = sqlite3.connect(db)
    tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert len(tablas) == TABLAS_DEL_GESTOR and "solicitudes_ingesta" not in tablas
    assert con.execute("PRAGMA user_version").fetchone()[0] == L.VERSION_ESQUEMA_DB
    con.close()


def test_mensajes_analizados_tiene_clave_compuesta(db):
    con = sqlite3.connect(db)
    pk = [f[1] for f in sorted((f for f in con.execute("PRAGMA table_info(mensajes_analizados)") if f[5] > 0), key=lambda f: f[5])]
    con.close()
    assert pk == ["run_id", "source_id"]


def test_prioridad_de_la_ruta_parametro_dataset_sqlite_y_constante(monkeypatch, tmp_path):
    monkeypatch.delenv("COMMUNITYLAB_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    monkeypatch.setattr(L, "DB_PATH", str(tmp_path / "const.db"))
    assert L._resolver_db_path() == str(tmp_path / "const.db")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "sqlite.db"))
    assert L._resolver_db_path() == str(tmp_path / "sqlite.db")
    monkeypatch.setenv("COMMUNITYLAB_DATA_DIR", str(tmp_path / "datos"))
    assert L._resolver_db_path() == str(tmp_path / "datos" / "communitylab.db")  # gana a SQLITE_DB_PATH
    assert L._resolver_db_path(tmp_path / "explicita.db") == str(tmp_path / "explicita.db")


def test_misma_ruta_que_la_api(monkeypatch, tmp_path):
    from api.config import Settings

    monkeypatch.setenv("COMMUNITYLAB_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "otra.db"))
    cfg = Settings(webhook_secret="s" * 20, _env_file=None)
    assert L._resolver_db_path() == str(cfg.db_path)


def test_coexiste_con_el_registro_de_la_api_bajo_escrituras_concurrentes(tmp_path):
    ruta = tmp_path / "communitylab.db"
    L.inicializar_db(ruta)
    registro = RegistroIdempotencia(ruta)
    registro.inicializar()
    errores: list[str] = []

    def api():
        try:
            for i in range(100):
                registro.registrar(f"r{i}", "a" * 64)
        except Exception as e:  # noqa: BLE001
            errores.append(f"api: {e!r}")

    def worker():
        try:
            for i in range(15):
                ids = [f"s{i}_{k}" for k in range(3)]
                L.persistir_resultado(**_todo(f"run-{i}", f"q{i}", ids), db_path=ruta)
        except Exception as e:  # noqa: BLE001
            errores.append(f"worker: {e!r}")

    hilos = [threading.Thread(target=api), threading.Thread(target=worker)]
    [h.start() for h in hilos]
    [h.join() for h in hilos]
    assert errores == []
    assert _n(ruta, "solicitudes_ingesta") == 100 and _n(ruta, "paquetes_salida") == 15


# --- clave compuesta: el defecto crítico ---------------------------------------


def test_mismo_source_id_en_dos_runs_no_se_pisa_ni_vacia_el_paquete_del_primero(db):
    """Antes: el segundo run movía el mensaje y borraba los source_ids del paquete del primero."""
    L.persistir_resultado(**_todo("run-1", "req-1", ["msg_001"]), db_path=db)
    L.persistir_resultado(**_todo("run-2", "req-2", ["msg_001"]), db_path=db)
    assert _n(db, "mensajes_analizados", "source_id='msg_001'") == 2
    assert _n(db, "post_linkedin_source_ids", "run_id='run-1'") == 1
    primero = L.reconstruir_json_paquete_salida("run-1", 1, db_path=db)
    assert primero["activos"]["post_linkedin"]["source_ids"] == ["msg_001"]


def test_borrar_un_mensaje_citado_por_un_paquete_falla_en_vez_de_vaciar_el_respaldo(db):
    L.persistir_resultado(**_todo("run-1", "req-1", ["m1", "m2"]), db_path=db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM mensajes_analizados WHERE run_id='run-1' AND source_id='m1'")
    con.close()
    assert _n(db, "post_linkedin_source_ids") == 2


def test_borrar_el_lote_arrastra_todo_lo_que_depende_de_el(db):
    L.persistir_resultado(**_todo("run-1", "req-1", ["m1", "m2"]), db_path=db)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("DELETE FROM lotes_procesamiento WHERE run_id='run-1'")
    con.commit()
    con.close()
    for tabla in ("mensajes_analizados", "mensaje_temas", "paquetes_salida", "post_linkedin_source_ids",
                  "resumen_semanal_source_ids", "estado_revision", "telemetria_llamadas_llm"):
        assert _n(db, tabla) == 0, tabla


# --- persistir_resultado: atomicidad e idempotencia ----------------------------


def test_completado_persiste_todo_y_reporta_conteos(db):
    kw = _todo("run-1", "req-1", ["m1", "m2", "m3"], no_aptos=("m3",))
    r = L.persistir_resultado(**kw, db_path=db)
    assert (r.mensajes_sanos, r.mensajes_dlq, r.rechazados_validador, r.revision_paquete) == (3, 0, 0, 1)
    for tabla, esperado in {"lotes_procesamiento": 1, "mensajes_analizados": 3, "mensaje_temas": 3,
                            "paquetes_salida": 1, "post_linkedin_source_ids": 2,
                            "telemetria_llamadas_llm": 1, "estado_revision": 1}.items():
        assert _n(db, tabla) == esperado, tabla


def test_reejecutar_el_mismo_run_deja_exactamente_el_mismo_estado(db):
    kw = _todo("run-1", "req-1", ["m1", "m2", "m3"], no_aptos=("m3",))
    L.persistir_resultado(**kw, db_path=db)
    antes = {t: _n(db, t) for t in ("mensajes_analizados", "mensaje_temas", "mensaje_entidades", "paquetes_salida",
                                    "post_linkedin_hashtags", "post_linkedin_source_ids", "resumen_temas_principales",
                                    "telemetria_llamadas_llm", "auditoria_rechazados")}
    json1 = L.reconstruir_json_paquete_salida("run-1", 1, db_path=db)
    L.persistir_resultado(**kw, db_path=db)
    L.persistir_resultado(**kw, db_path=db)
    assert {t: _n(db, t) for t in antes} == antes
    assert L.reconstruir_json_paquete_salida("run-1", 1, db_path=db) == json1


def test_total_rechazados_del_lote_es_el_del_validador_no_el_del_analisis(db):
    kw = _todo("run-1", "req-1", ["a", "b"])
    kw["rechazados_validador"] = [{"mensaje_id": "x", "error_detalle": "mal"}, {"mensaje_id": "y", "error_detalle": "mal"}]
    kw["total_recibidos"] = 4
    L.persistir_resultado(**kw, db_path=db)
    fila = sqlite3.connect(db).execute(
        "SELECT total_recibidos, total_validos, total_rechazados FROM lotes_procesamiento").fetchone()
    assert fila == (4, 2, 2)


def test_sin_registros_validos_persiste_solo_lote_y_rechazos(db):
    rech = [{"mensaje_id": "x", "autor": "A", "canal": "c", "error_detalle": "vacío", "texto_crudo": ""}]
    r = L.persistir_resultado(run_id="run-v", request_id="req-v", origen_comunidad="Discord",
                              periodo_referencia="2026-W42", total_recibidos=1, total_validos=0,
                              rechazados_validador=rech, db_path=db)
    assert (r.mensajes_sanos, r.rechazados_validador, r.revision_paquete) == (0, 1, None)
    assert _n(db, "lotes_procesamiento") == 1 and _n(db, "auditoria_rechazados", "origen='validador'") == 1
    assert _n(db, "mensajes_analizados") == 0 and _n(db, "paquetes_salida") == 0


def test_sin_contenido_publicable_persiste_analisis_sin_paquete(db):
    kw = _todo("run-s", "req-s", ["m1", "m2"], no_aptos=("m1", "m2"))
    kw["paquete"] = None
    r = L.persistir_resultado(**kw, db_path=db)
    assert r.revision_paquete is None and _n(db, "mensajes_analizados", "apto_para_publicacion=0") == 2
    assert _n(db, "paquetes_salida") == 0


def test_si_algo_falla_se_revierte_el_run_completo(db):
    """El paquete cita un source_id que el análisis mandó a la DLQ -> IntegrityError -> nada queda."""
    ids = ["m1", "m2"]
    kw = _todo("run-x", "req-x", ids)
    kw["paquete"] = _paquete("run-x", "req-x", ids, citados=["fantasma", "m1"])
    with pytest.raises(sqlite3.IntegrityError):
        L.persistir_resultado(**kw, db_path=db)
    for tabla in ("lotes_procesamiento", "mensajes_analizados", "paquetes_salida", "telemetria_llamadas_llm"):
        assert _n(db, tabla) == 0, tabla


def test_un_fallo_en_una_reejecucion_conserva_el_resultado_anterior(db):
    ids = ["m1", "m2"]
    L.persistir_resultado(**_todo("run-x", "req-x", ids), db_path=db)
    antes = L.reconstruir_json_paquete_salida("run-x", 1, db_path=db)
    malo = _todo("run-x", "req-x", ids)
    malo["paquete"] = _paquete("run-x", "req-x", ids, citados=["fantasma"])
    with pytest.raises(sqlite3.IntegrityError):
        L.persistir_resultado(**malo, db_path=db)
    assert L.reconstruir_json_paquete_salida("run-x", 1, db_path=db) == antes


@pytest.mark.parametrize("campo", ["analisis_cognitivo", "paquete"])
def test_rechaza_artefactos_de_otro_run(db, campo):
    kw = _todo("run-1", "req-1", ["m1"])
    kw[campo] = (_analisis("run-OTRO", "req-1", ["m1"]) if campo == "analisis_cognitivo"
                 else _paquete("run-OTRO", "req-1", ["m1"]))
    with pytest.raises(ValueError, match="otro run_id"):
        L.persistir_resultado(**kw, db_path=db)
    assert _n(db, "lotes_procesamiento") == 0


def test_dos_revisiones_del_mismo_run_conviven(db):
    ids = ["m1", "m2"]
    kw = _todo("run-1", "req-1", ids)
    L.persistir_resultado(**kw, db_path=db)
    L.registrar_paquete_salida(_paquete("run-1", "req-1", ids, rev=2), "Discord", "2026-W42", db_path=db)
    assert _n(db, "paquetes_salida") == 2
    assert L.reconstruir_json_paquete_salida("run-1", 2, db_path=db)["revision"]["numero_revision"] == 2


# --- DLQ y rechazados del validador --------------------------------------------


def test_id_huerfano_del_analisis_va_a_la_dlq_con_origen_analisis(db):
    validos, _ = validar_lote_crudo(_lote("req-1", ["m1"]), run_id="run-1")
    analisis = _analisis("run-1", "req-1", ["m1", "fantasma"])
    L.registrar_lote_procesamiento("run-1", "req-1", "Discord", "2026-W42", 1, 1, 0, db_path=db)
    assert L.registrar_analisis_bloque(analisis, validos, db_path=db) == (1, 1)
    con = sqlite3.connect(db)
    fila = con.execute("SELECT origen, mensaje_id, error_detalle FROM auditoria_rechazados").fetchone()
    con.close()
    assert fila[0] == "analisis" and fila[1] == "fantasma" and fila[2].startswith("ERROR_INTEGRIDAD")


def test_rechazados_del_validador_exigen_run_id(db):
    with pytest.raises(ValueError, match="run_id"):
        L.registrar_rechazados_validador([{"mensaje_id": "x", "error_detalle": "e"}], db_path=db)
    assert _n(db, "auditoria_rechazados") == 0


def test_rechazados_del_validador_son_idempotentes_y_no_tocan_los_del_analisis(db):
    validos, _ = validar_lote_crudo(_lote("req-1", ["m1"]), run_id="run-1")
    L.registrar_lote_procesamiento("run-1", "req-1", "Discord", "2026-W42", 3, 1, 2, db_path=db)
    L.registrar_analisis_bloque(_analisis("run-1", "req-1", ["m1", "fantasma"]), validos, db_path=db)
    rech = [{"run_id": "run-1", "request_id": "req-1", "mensaje_id": "a", "error_detalle": "e1"},
            {"run_id": "run-1", "request_id": "req-1", "mensaje_id": "b", "error_detalle": "e2"}]
    for _ in range(3):
        assert L.registrar_rechazados_validador(rech, db_path=db) == 2
    assert _n(db, "auditoria_rechazados", "origen='validador'") == 2
    assert _n(db, "auditoria_rechazados", "origen='analisis'") == 1


def test_totales_provisionales_por_run_cuando_el_lote_aun_no_existe(db):
    rech = [{"run_id": "run-A", "request_id": "qa", "mensaje_id": "1", "error_detalle": "e"},
            {"run_id": "run-A", "request_id": "qa", "mensaje_id": "2", "error_detalle": "e"},
            {"run_id": "run-B", "request_id": "qb", "mensaje_id": "3", "error_detalle": "e"}]
    L.registrar_rechazados_validador(rech, db_path=db)
    con = sqlite3.connect(db)
    filas = dict(con.execute("SELECT run_id, total_rechazados FROM lotes_procesamiento").fetchall())
    con.close()
    assert filas == {"run-A": 2, "run-B": 1}  # antes: 3 en ambos (conteo global)


# --- telemetría ----------------------------------------------------------------


def test_telemetria_se_reemplaza_por_run_y_guarda_los_tokens(db):
    L.persistir_resultado(**_todo("run-1", "req-1", ["m1"]), db_path=db)
    assert L.registrar_telemetria("run-1", "req-1", [_telemetria(), _telemetria(modelo="otro", tokens_totales=5)], db_path=db) == 2
    L.registrar_telemetria("run-1", "req-1", [_telemetria()], db_path=db)
    con = sqlite3.connect(db)
    fila = con.execute("SELECT proveedor, modelo, latencia_ms, prompt_tokens, completion_tokens, tokens_totales, "
                       "tokens_cacheados FROM telemetria_llamadas_llm").fetchall()
    con.close()
    assert fila == [("openai", "gpt-x", 100, 50, 30, 80, 10)]


def test_acepta_la_telemetria_real_del_modulo_cognitivo(db):
    from ai_modules.cognitive_analyzer import TelemetriaAnalisis

    t = TelemetriaAnalisis(proveedor="openai", modelo="gpt-x", prompt_version="1.0.0+abc", latencia_ms=900,
                           tokens_entrada=100, tokens_salida=40, tokens_cacheados=20,
                           total_procesados=3, degradados=0)
    kw = _todo("run-1", "req-1", ["m1"])
    kw["telemetria"] = [t]
    L.persistir_resultado(**kw, db_path=db)
    assert sqlite3.connect(db).execute("SELECT tokens_totales FROM telemetria_llamadas_llm").fetchone()[0] == 140


# --- reconstrucción del Contrato 3 ----------------------------------------------


def test_ida_y_vuelta_el_json_reconstruido_es_identico_al_del_paquete_original(db):
    """Clave para OCI: la comprobación de idempotencia compara el contenido del objeto."""
    kw = _todo("run-1", "req-1", ["m1", "m2", "m3"], no_aptos=("m3",))
    L.persistir_resultado(**kw, db_path=db)
    assert L.reconstruir_json_paquete_salida("run-1", 1, db_path=db) == kw["paquete"].model_dump(mode="json")


def test_reconstruccion_valida_contra_paquete_salida_y_es_determinista(db):
    kw = _todo("run-1", "req-1", ["m3", "m1", "m2"], no_aptos=("m3", "m1"))
    L.persistir_resultado(**kw, db_path=db)
    a = L.reconstruir_json_paquete_salida("run-1", 1, db_path=db)
    b = L.reconstruir_json_paquete_salida("run-1", 1, db_path=db)
    assert a == b and PaqueteSalida.model_validate(a)
    assert [c["source_id"] for c in a["activos"]["casos_no_publicables"]] == ["m1", "m3"]


def test_paquete_inexistente_lanza_value_error(db):
    with pytest.raises(ValueError, match="No se encontró"):
        L.reconstruir_json_paquete_salida("run-nada", 1, db_path=db)


def test_una_base_inconsistente_no_se_maquilla_se_lanza_value_error(db):
    L.persistir_resultado(**_todo("run-1", "req-1", ["m1"]), db_path=db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM resumen_temas_principales WHERE run_id='run-1'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="no cumple el Contrato 3"):
        L.reconstruir_json_paquete_salida("run-1", 1, db_path=db)


# --- sincronización con OCI ------------------------------------------------------


class _ClienteOCI:
    def __init__(self, ok: bool = True):
        self.ok, self.recibidos = ok, []

    def upload_package(self, package_payload, periodo_referencia, run_id, numero_revision, request_id):
        self.recibidos.append((package_payload, periodo_referencia, run_id, numero_revision, request_id))
        return SimpleNamespace(status_almacenamiento="guardado_con_exito" if self.ok else "guardado_error",
                               comprobacion_lectura=self.ok, mensaje_error=None if self.ok else "sin red")


def _con_pendiente(db, run="run-1"):
    kw = _todo(run, "req-" + run, ["m1", "m2"])
    kw["paquete"] = _paquete(run, "req-" + run, ["m1", "m2"], status="guardado_error")
    L.persistir_resultado(**kw, db_path=db)


def test_sincronizar_sube_el_paquete_reconstruido_y_marca_el_exito(db):
    _con_pendiente(db)
    cliente = _ClienteOCI()
    assert L.sincronizar_pendientes_oci(cliente, db_path=db) == (1, 0)
    payload, periodo, run_id, rev, req = cliente.recibidos[0]
    assert (periodo, run_id, rev, req) == ("2026-W42", "run-1", 1, "req-run-1")
    # El reintento sube la evidencia en su estado inicial (el cliente de OCI exige que no
    # llegue precompuesta); el éxito se registra después en la base.
    assert payload["almacenamiento_oci"]["status_almacenamiento"] == "no_iniciado"
    assert payload["almacenamiento_oci"]["comprobacion_lectura"] is False
    fila =sqlite3.connect(db).execute("SELECT status_almacenamiento, comprobacion_lectura FROM evidencia_almacenamiento_oci").fetchone()
    assert fila == ("guardado_con_exito", 1)
    assert L.sincronizar_pendientes_oci(cliente, db_path=db) == (0, 0)  # ya no queda nada


def test_sincronizar_deja_pendiente_lo_que_falla(db):
    _con_pendiente(db)
    assert L.sincronizar_pendientes_oci(_ClienteOCI(ok=False), db_path=db) == (0, 1)
    assert _n(db, "evidencia_almacenamiento_oci", "status_almacenamiento='guardado_error'") == 1


def test_sincronizar_no_sube_un_paquete_inconsistente(db):
    _con_pendiente(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM resumen_temas_principales")
    con.commit()
    con.close()
    cliente = _ClienteOCI()
    assert L.sincronizar_pendientes_oci(cliente, db_path=db) == (0, 1)
    assert cliente.recibidos == []


# --- migración v1 -> v2 -----------------------------------------------------------

_DDL_V1 = """
CREATE TABLE lotes_procesamiento (run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, origen_comunidad TEXT NOT NULL,
    periodo_referencia TEXT NOT NULL, total_recibidos INTEGER NOT NULL, total_validos INTEGER NOT NULL,
    total_rechazados INTEGER NOT NULL, fecha_procesamiento TEXT NOT NULL);
CREATE TABLE mensajes_analizados (source_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, autor TEXT NOT NULL,
    canal TEXT NOT NULL, tipo TEXT NOT NULL, texto_limpio TEXT NOT NULL, sentimiento TEXT NOT NULL,
    categoria_enrutamiento TEXT NOT NULL, regla_aplicada TEXT NOT NULL, evidencia_explicita INTEGER NOT NULL,
    utilidad_comunitaria INTEGER NOT NULL, claridad_contexto INTEGER NOT NULL, puntuacion_total INTEGER NOT NULL,
    motivo_seleccion TEXT NOT NULL, requiere_soporte INTEGER NOT NULL DEFAULT 0, apto_para_publicacion INTEGER NOT NULL,
    fecha_analisis TEXT NOT NULL, FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE);
CREATE TABLE mensaje_temas (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, tema TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE);
CREATE TABLE mensaje_entidades (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, entidad TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE);
CREATE TABLE auditoria_rechazados (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, request_id TEXT NOT NULL,
    mensaje_id TEXT, autor TEXT, canal TEXT, error_detalle TEXT NOT NULL, texto_crudo TEXT, fecha_aislamiento TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE);
"""


def test_migra_una_base_v1_conservando_los_datos(tmp_path):
    ruta = tmp_path / "v1.db"
    con = sqlite3.connect(ruta)
    con.executescript(_DDL_V1)
    con.execute("INSERT INTO lotes_procesamiento VALUES ('run-1','req-1','Discord','2026-W42',2,2,0,'2026-10-12')")
    con.execute("INSERT INTO mensajes_analizados VALUES ('m1','run-1','Ana','c','mensaje','hola','Positivo','Logro','r',2,2,2,6,"
                "'motivo suficiente largo',0,1,'2026-10-12')")
    con.execute("INSERT INTO mensaje_temas (source_id, tema) VALUES ('m1','python')")
    con.execute("INSERT INTO mensaje_entidades (source_id, entidad) VALUES ('m1','pandas')")
    con.execute("INSERT INTO auditoria_rechazados (run_id, request_id, mensaje_id, error_detalle, fecha_aislamiento) "
                "VALUES ('run-1','req-1','x','ERROR_INTEGRIDAD: huérfano','2026-10-12'),('run-1','req-1','y','texto vacío','2026-10-12')")
    con.commit()
    con.close()

    L.inicializar_db(ruta)
    L.inicializar_db(ruta)  # idempotente

    con = sqlite3.connect(ruta)
    assert con.execute("SELECT run_id, source_id, texto_limpio FROM mensajes_analizados").fetchall() == [("run-1", "m1", "hola")]
    assert con.execute("SELECT run_id, source_id, tema FROM mensaje_temas").fetchall() == [("run-1", "m1", "python")]
    assert con.execute("SELECT run_id, source_id, entidad FROM mensaje_entidades").fetchall() == [("run-1", "m1", "pandas")]
    assert dict(con.execute("SELECT mensaje_id, origen FROM auditoria_rechazados").fetchall()) == {"x": "analisis", "y": "validador"}
    assert con.execute("PRAGMA user_version").fetchone()[0] == L.VERSION_ESQUEMA_DB
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()
    # ...y la base migrada ya admite el mismo source_id en otro run.
    L.persistir_resultado(**_todo("run-2", "req-2", ["m1"]), db_path=ruta)
    assert _n(ruta, "mensajes_analizados", "source_id='m1'") == 2


# --- fachada LocalDatabaseManager ---------------------------------------------------

def test_manager_usa_una_ruta_fija_y_persiste_y_reconstruye(tmp_path):
    m = L.LocalDatabaseManager(tmp_path / "m.db")
    kw = _todo("run-m", "req-m", ["m1", "m2"])
    kw.pop("db_path", None)
    assert m.persistir_resultado(**kw).revision_paquete == 1
    assert m.reconstruir_json_paquete_salida("run-m", 1) == kw["paquete"].model_dump(mode="json")


def test_manager_sin_argumentos_respeta_communitylab_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMUNITYLAB_DATA_DIR", str(tmp_path / "datos"))
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "otra.db"))
    m = L.LocalDatabaseManager()
    assert m.db_path == str(tmp_path / "datos" / "communitylab.db")


# --- reconciliación con la versión de Andrea (local_db_2) ----------------------------


def test_las_llamadas_antiguas_sin_ruta_siguen_funcionando_con_db_path_parcheado(tmp_path, monkeypatch):
    """Firma heredada: `inicializar_db()` y `registrar_analisis_bloque(analisis, origen)` sin `db_path`."""
    monkeypatch.delenv("COMMUNITYLAB_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
    ruta = tmp_path / "legacy" / "c.db"
    monkeypatch.setattr(L, "DB_PATH", str(ruta))
    L.inicializar_db()
    validos, _ = validar_lote_crudo(_lote("req-l", ["m1", "m2"]), run_id="run-l")
    assert L.registrar_analisis_bloque(_analisis("run-l", "req-l", ["m1", "m2"]), validos) == (2, 0)
    assert _n(str(ruta), "mensajes_analizados") == 2


def test_motivo_corto_va_a_la_dlq_con_el_prefijo_de_negocio(db):
    """Un mensaje con `motivo_seleccion` < 10 (saltándose Pydantic) no rompe el lote: va a la DLQ."""
    validos, _ = validar_lote_crudo(_lote("req-c", ["m1", "m2"]), run_id="run-c")
    analisis = _analisis("run-c", "req-c", ["m1", "m2"])
    corto = analisis.mensajes_analizados[1].model_copy(update={"motivo_seleccion": "corto"})
    analisis = analisis.model_copy(update={"mensajes_analizados": [analisis.mensajes_analizados[0], corto]})
    L.inicializar_db(db)
    assert L.registrar_analisis_bloque(analisis, validos, db_path=db) == (1, 1)
    con = sqlite3.connect(db)
    fila = con.execute("SELECT mensaje_id, error_detalle, origen FROM auditoria_rechazados").fetchone()
    con.close()
    assert fila[0] == "m2" and fila[1].startswith("ERROR_NEGOCIO_CHECK:") and fila[2] == "analisis"


def test_lote_vacio_se_admite_pero_los_totales_negativos_no(db):
    L.inicializar_db(db)
    con = sqlite3.connect(db)
    cols = "(run_id, request_id, origen_comunidad, periodo_referencia, total_recibidos, total_validos, total_rechazados, fecha_procesamiento)"
    con.execute(f"INSERT INTO lotes_procesamiento {cols} VALUES ('r0','q0','Discord','2026-W42',0,0,0,'2026-10-12')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(f"INSERT INTO lotes_procesamiento {cols} VALUES ('r1','q1','Discord','2026-W42',-1,0,0,'2026-10-12')")
    con.close()


def test_una_base_creada_con_el_ddl_con_typo_fecha_isolation_se_repara_y_admite_rechazos(tmp_path):
    """Versión intermedia: la columna se llamaba `fecha_isolation` y los INSERT usaban otro nombre."""
    ruta = tmp_path / "typo.db"
    con = sqlite3.connect(ruta)
    con.executescript(_DDL_V1.replace("fecha_aislamiento", "fecha_isolation"))
    con.execute("INSERT INTO lotes_procesamiento VALUES ('run-1','req-1','Discord','2026-W42',1,1,0,'2026-10-12')")
    con.execute("INSERT INTO auditoria_rechazados (run_id, request_id, mensaje_id, error_detalle, fecha_isolation) "
                "VALUES ('run-1','req-1','x','ERROR_NEGOCIO_CHECK: corto','2026-10-12')")
    con.commit()
    con.close()

    L.inicializar_db(ruta)

    con = sqlite3.connect(ruta)
    columnas = [c[1] for c in con.execute("PRAGMA table_info(auditoria_rechazados)")]
    assert "fecha_aislamiento" in columnas and "fecha_isolation" not in columnas
    assert con.execute("SELECT origen FROM auditoria_rechazados").fetchone() == ("analisis",)
    con.close()
    rech = [{"run_id": "run-1", "request_id": "req-1", "mensaje_id": "y", "error": "texto vacío"}]
    assert L.registrar_rechazados_validador(rech, db_path=ruta) == 1


def test_una_reejecucion_no_borra_las_decisiones_de_curaduria_de_otras_revisiones(db):
    """La versión anterior purgaba el lote entero (DELETE + CASCADE) y arrasaba paquetes y revisiones."""
    kw = _todo("run-k", "req-k", ["m1", "m2"])
    L.persistir_resultado(**kw, db_path=db)
    con = sqlite3.connect(db)
    con.execute("UPDATE estado_revision SET estado='aprobado', revisor='Sergio' WHERE run_id='run-k'")
    con.commit()
    con.close()
    validos, _ = validar_lote_crudo(_lote("req-k", ["m1", "m2"]), run_id="run-k")
    L.registrar_analisis_bloque(_analisis("run-k", "req-k", ["m1", "m2"]), validos, db_path=db)
    assert _n(db, "estado_revision", "estado='aprobado' AND revisor='Sergio'") == 1
    assert _n(db, "paquetes_salida") == 1


# --- métodos de LocalDatabaseManager que usa orchestration/pipeline_runner.py --------------------


def _manager(tmp_path):
    return L.LocalDatabaseManager(tmp_path / "m.db")


def test_registrar_lote_hace_upsert_y_reemplaza_solo_los_rechazos_cuando_se_le_pasan(tmp_path):
    m = _manager(tmp_path)
    rech = [{"run_id": "run-1", "request_id": "req-1", "mensaje_id": "x", "error": "vacío"}]
    m.registrar_lote("run-1", "req-1", "Discord", "2026-W42", 3, 2, 1, rech)
    m.registrar_lote("run-1", "req-1", "Discord", "2026-W42", 3, 2, 1, rech)  # reintento: no duplica
    assert _n(m.db_path, "auditoria_rechazados") == 1 and _n(m.db_path, "lotes_procesamiento") == 1
    m.registrar_lote("run-1", "req-1", "Discord", "2026-W42", 3, 0, 1)  # rechazados=None: no los toca
    assert _n(m.db_path, "auditoria_rechazados") == 1
    assert _n(m.db_path, "lotes_procesamiento", "total_validos=0") == 1


def test_registrar_lote_y_analisis_persiste_mensajes_y_resumen(tmp_path):
    m = _manager(tmp_path)
    validos, _ = validar_lote_crudo(_lote("req-2", ["m1", "m2"]), run_id="run-2")
    assert m.registrar_lote_y_analisis(_analisis("run-2", "req-2", ["m1", "m2"]), validos, "Discord", "2026-W42", 2, 0) == (2, 0)
    assert _n(m.db_path, "mensajes_analizados") == 2 and _n(m.db_path, "resumen_comunidad") == 1


def test_registrar_paquete_sin_origen_ni_periodo_usa_el_lote_ya_registrado(tmp_path):
    m = _manager(tmp_path)
    ids = ["m1", "m2"]
    validos, _ = validar_lote_crudo(_lote("req-3", ids), run_id="run-3")
    m.registrar_lote("run-3", "req-3", "Discord", "2026-W42", 2, 2, 0, [])
    m.registrar_lote_y_analisis(_analisis("run-3", "req-3", ids), validos, "Discord", "2026-W42", 2, 0)
    assert m.registrar_paquete(_paquete("run-3", "req-3", ids)) == 1
    # El encabezado del lote conserva los datos reales (el paquete no los pisa con "Desconocido").
    con = sqlite3.connect(m.db_path)
    assert con.execute("SELECT origen_comunidad, periodo_referencia FROM lotes_procesamiento").fetchone() == ("Discord", "2026-W42")
    con.close()
    assert m.reconstruir_json_paquete_salida("run-3", 1)["run_id"] == "run-3"


def test_leer_rechazados_devuelve_solo_los_del_validador_con_las_llaves_del_contrato(tmp_path):
    m = _manager(tmp_path)
    rech = [{"run_id": "run-4", "request_id": "req-4", "mensaje_id": "x", "autor": "Ana", "canal": "c",
             "error": "texto vacío", "texto_crudo": "  "}]
    m.registrar_lote("run-4", "req-4", "Discord", "2026-W42", 1, 0, 1, rech)
    filas = m.leer_rechazados("run-4")
    assert len(filas) == 1
    assert {"id", "mensaje_id", "error", "error_detalle", "run_id", "request_id", "autor", "canal"} <= set(filas[0])
    assert filas[0]["error_detalle"] == "texto vacío" and m.leer_rechazados("otro") == []


def test_actualizar_estado_flujo_guarda_el_estado_y_devuelve_false_si_el_run_no_existe(tmp_path):
    m = _manager(tmp_path)
    m.registrar_lote("run-5", "req-5", "Discord", "2026-W42", 1, 1, 0, [])
    assert _n(m.db_path, "lotes_procesamiento", "estado_flujo='iniciado'") == 1  # valor por defecto
    assert m.actualizar_estado_flujo("run-5", "completado") is True
    assert _n(m.db_path, "lotes_procesamiento", "estado_flujo='completado'") == 1
    assert m.actualizar_estado_flujo("no-existe", "fallido") is False


def test_una_reejecucion_del_lote_no_borra_el_estado_de_flujo_ya_registrado(tmp_path):
    m = _manager(tmp_path)
    m.registrar_lote("run-6", "req-6", "Discord", "2026-W42", 1, 1, 0, [])
    m.actualizar_estado_flujo("run-6", "completado")
    m.registrar_lote("run-6", "req-6", "Discord", "2026-W42", 1, 1, 0, [])
    assert _n(m.db_path, "lotes_procesamiento", "estado_flujo='completado'") == 1


def test_inicializar_agrega_estado_flujo_a_una_base_v2_que_no_lo_tenia(tmp_path):
    ruta = tmp_path / "v2sin.db"
    L.inicializar_db(ruta)
    con = sqlite3.connect(ruta)
    con.execute("ALTER TABLE lotes_procesamiento DROP COLUMN estado_flujo")
    con.execute("INSERT INTO lotes_procesamiento (run_id, request_id, origen_comunidad, periodo_referencia, "
                "total_recibidos, total_validos, total_rechazados, fecha_procesamiento) "
                "VALUES ('r','q','Discord','2026-W42',1,1,0,'2026-10-12')")
    con.commit()
    con.close()
    L.inicializar_db(ruta)
    con = sqlite3.connect(ruta)
    assert con.execute("SELECT estado_flujo FROM lotes_procesamiento").fetchone() == ("iniciado",)
    con.close()


def test_manager_registrar_telemetria_reemplaza_por_run(db):
    m = L.LocalDatabaseManager(db)
    m.registrar_lote("run-t", "req-t", "Discord", "2026-W42", 2, 2, 0)
    assert m.registrar_telemetria("run-t", "req-t", [_telemetria(), _telemetria(modelo="gpt-y")]) == 2
    assert m.registrar_telemetria("run-t", "req-t", [_telemetria()]) == 1  # reemplaza, no acumula
    assert _n(db, "telemetria_llamadas_llm", "run_id='run-t'") == 1
