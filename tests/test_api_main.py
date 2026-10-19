"""Pruebas de la API perimetral (api/main.py) y su registro de idempotencia.

Se ejecutan con `TestClient` sobre una base SQLite y un `data/raw` temporales:
no tocan datos reales ni requieren red.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.main as api_main
from api.config import Settings
from api.logging_config import redactar
from api.main import create_app, escribir_lote_atomico, huella_lote
from api.registro import (
    ConflictoIdempotencia,
    RegistroIdempotencia,
    generar_run_id,
)
from esquemas import IngestionLote

SECRETO = "secreto-de-prueba-1234567890"
URL = "/api/v1/interactions/process"
H = {"X-Webhook-Secret": SECRETO}


def _lote(request_id: str = "req-001", texto: str = "Terminé mi primer proyecto") -> dict:
    return {
        "schema_version": "1.1.0",
        "request_id": request_id,
        "origen_comunidad": "Discord",
        "periodo_referencia": "2026-W42",
        "interacciones": [
            {
                "id": "msg_001",
                "autor": "Ana",
                "canal": "logros",
                "tipo": "mensaje",
                "texto": texto,
                "metadata_origen": {
                    "plataforma": "Discord",
                    "identificador_original": "d-1",
                    "fecha": "2026-10-12T10:00:00Z",
                },
            }
        ],
    }


def _settings(tmp_path, **extra) -> Settings:
    return Settings(
        webhook_secret=SECRETO, data_dir=tmp_path / "data", _env_file=None, **extra
    )


@pytest.fixture
def cliente(tmp_path):
    cfg = _settings(tmp_path)
    with TestClient(create_app(cfg)) as c:
        c.cfg = cfg  # type: ignore[attr-defined]
        yield c


def _archivos_raw(cliente):
    return sorted(p.name for p in cliente.cfg.raw_dir.iterdir())


def test_arranque_falla_sin_secreto(monkeypatch, tmp_path):
    monkeypatch.delenv("COMMUNITYLAB_WEBHOOK_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, _env_file=None)


def test_arranque_falla_con_secreto_corto(tmp_path):
    with pytest.raises(ValidationError):
        Settings(webhook_secret="corto", data_dir=tmp_path, _env_file=None)


def test_secreto_no_aparece_en_repr_de_settings(tmp_path):
    assert SECRETO not in repr(_settings(tmp_path))


def test_docs_apagados_por_defecto(cliente):
    assert cliente.get("/docs").status_code == 404
    assert cliente.get("/openapi.json").status_code == 404


def test_openapi_se_genera_cuando_se_habilita(tmp_path):
    with TestClient(create_app(_settings(tmp_path, habilitar_docs=True))) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        assert URL in r.json()["paths"]


@pytest.mark.parametrize(
    "cabeceras",
    [{}, {"X-Webhook-Secret": "incorrecto"}, {"X-Webhook-Secret": "ñ-no-ascii".encode()}],
)
def test_post_sin_secreto_valido_devuelve_401_y_no_escribe(cliente, cabeceras):
    r = cliente.post(URL, json=_lote(), headers=cabeceras)
    assert r.status_code == 401
    assert _archivos_raw(cliente) == []


def test_401_antes_que_validar_el_cuerpo(cliente):
    r = cliente.post(URL, content=b"no es json", headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_json_malformado_devuelve_422(cliente):
    r = cliente.post(URL, content=b"{no json", headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 422


def test_sobre_sin_request_id_devuelve_422(cliente):
    cuerpo = _lote()
    del cuerpo["request_id"]
    assert cliente.post(URL, json=cuerpo, headers=H).status_code == 422


def test_payload_plano_oficial_del_jurado_se_adapta_y_acepta_con_202(cliente):
    """Mitigación Riesgo 1: si se envía el JSON plano del jurado (sin request_id,
    sin id y sin metadata_origen en sus ítems), la API lo adapta con
    `adaptar_payload_oficial()` y deposita un `IngestionLote` válido en `data/raw/`.
    """
    payload_jurado = {
        "origen_comunidad": "Discord_Grupo_ONE_G10",
        "periodo_referencia": "Semana_04",
        "interacciones": [
            {
                "autor": "Mariana Souza",
                "canal": "#logros-y-empleos",
                "tipo": "testimonio",
                "texto": "Quedé seleccionada como Desarrolladora Junior de IA!",
            }
        ],
    }
    r = cliente.post(URL, json=payload_jurado, headers=H)
    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["request_id"].startswith("req_")
    assert cuerpo["estado"] == "en_cola"
    guardado = (cliente.cfg.raw_dir / f"{cuerpo['run_id']}.json").read_text(encoding="utf-8")
    lote = IngestionLote.model_validate_json(guardado)
    assert lote.interacciones[0].metadata_origen.plataforma == "Discord"


def test_lote_sin_interacciones_devuelve_422(cliente):
    cuerpo = _lote()
    cuerpo["interacciones"] = []
    assert cliente.post(URL, json=cuerpo, headers=H).status_code == 422
    assert _archivos_raw(cliente) == []


def test_422_no_devuelve_el_contenido_rechazado(cliente):
    cuerpo = _lote(texto="correo privado ana@example.com")
    cuerpo["interacciones"][0]["metadata_origen"] = "mal"
    r = cliente.post(URL, json=cuerpo, headers=H)
    assert r.status_code == 422
    assert "ana@example.com" not in r.text


@pytest.mark.parametrize("periodo", ["../etc", "a/b", "", "..", "con espacio"])
def test_periodo_referencia_inseguro_devuelve_422(cliente, periodo):
    cuerpo = _lote()
    cuerpo["periodo_referencia"] = periodo
    r = cliente.post(URL, json=cuerpo, headers=H)
    assert r.status_code == 422
    assert _archivos_raw(cliente) == []


def test_request_id_vacio_o_gigante_devuelve_422(cliente):
    assert cliente.post(URL, json=_lote(request_id="   "), headers=H).status_code == 422
    assert cliente.post(URL, json=_lote(request_id="x" * 201), headers=H).status_code == 422


def test_cuerpo_demasiado_grande_devuelve_413(tmp_path):
    cfg = _settings(tmp_path, max_body_bytes=500)
    with TestClient(create_app(cfg)) as c:
        assert c.post(URL, json=_lote(texto="x" * 2000), headers=H).status_code == 413

        def trozos():  # sin Content-Length: transferencia por trozos
            for _ in range(10):
                yield b"x" * 200

        r = c.post(URL, content=trozos(), headers={**H, "Content-Type": "application/json"})
        assert r.status_code == 413
        assert list(cfg.raw_dir.iterdir()) == []


def test_primer_envio_devuelve_202_y_deposita_el_archivo(cliente):
    r = cliente.post(URL, json=_lote(), headers=H)
    assert r.status_code == 202
    cuerpo = r.json()
    assert cuerpo["request_id"] == "req-001"
    assert cuerpo["estado"] == "en_cola"
    assert cuerpo["duplicado"] is False

    assert _archivos_raw(cliente) == [f"{cuerpo['run_id']}.json"]  # sin temp_* residuales
    guardado = (cliente.cfg.raw_dir / f"{cuerpo['run_id']}.json").read_text(encoding="utf-8")
    lote = IngestionLote.model_validate_json(guardado)
    assert lote.request_id == "req-001" and lote.periodo_referencia == "2026-W42"


def test_repeticion_identica_devuelve_200_con_el_mismo_run_id_sin_tocar_disco(cliente):
    primero = cliente.post(URL, json=_lote(), headers=H).json()
    archivo = cliente.cfg.raw_dir / f"{primero['run_id']}.json"
    mtime = archivo.stat().st_mtime_ns

    r = cliente.post(URL, json=_lote(), headers=H)
    assert r.status_code == 200
    assert r.json()["run_id"] == primero["run_id"]
    assert r.json()["duplicado"] is True
    assert _archivos_raw(cliente) == [archivo.name]
    assert archivo.stat().st_mtime_ns == mtime


def test_orden_de_claves_y_schema_version_implicito_no_cambian_la_huella(cliente):
    primero = cliente.post(URL, json=_lote(), headers=H).json()
    cuerpo = _lote()
    del cuerpo["schema_version"]  # el valor por defecto es el mismo
    invertido = dict(reversed(list(cuerpo.items())))
    r = cliente.post(
        URL, content=json.dumps(invertido), headers={**H, "Content-Type": "application/json"}
    )
    assert r.status_code == 200
    assert r.json()["run_id"] == primero["run_id"]


def test_mismo_request_id_con_otro_contenido_devuelve_409_y_no_sobrescribe(cliente):
    primero = cliente.post(URL, json=_lote(), headers=H).json()
    archivo = cliente.cfg.raw_dir / f"{primero['run_id']}.json"
    original = archivo.read_text(encoding="utf-8")

    r = cliente.post(URL, json=_lote(texto="Otro texto distinto"), headers=H)
    assert r.status_code == 409
    assert r.json()["error"] == "conflicto_idempotencia"
    assert archivo.read_text(encoding="utf-8") == original
    assert _archivos_raw(cliente) == [archivo.name]


def test_request_id_distinto_crea_otro_run(cliente):
    a = cliente.post(URL, json=_lote("req-a"), headers=H).json()
    b = cliente.post(URL, json=_lote("req-b"), headers=H).json()
    assert a["run_id"] != b["run_id"]
    assert len(_archivos_raw(cliente)) == 2


def test_duplicado_refleja_el_estado_actual_del_run(cliente):
    primero = cliente.post(URL, json=_lote(), headers=H).json()
    cliente.app.state.registro.actualizar_estado(primero["run_id"], "procesando")
    r = cliente.post(URL, json=_lote(), headers=H)
    assert r.status_code == 200 and r.json()["estado"] == "procesando"


def test_fallo_de_escritura_devuelve_503_y_permite_reintentar(cliente, monkeypatch):
    def falla(*_a, **_k):
        raise OSError("disco lleno")

    monkeypatch.setattr(api_main, "escribir_lote_atomico", falla)
    r = cliente.post(URL, json=_lote(), headers=H)
    assert r.status_code == 503 and r.headers["Retry-After"]

    monkeypatch.undo()
    r2 = cliente.post(URL, json=_lote(), headers=H)
    assert r2.status_code == 202  # no quedó un run fantasma bloqueando el request_id
    assert len(_archivos_raw(cliente)) == 1


def test_recepcion_en_curso_devuelve_503_y_tras_la_gracia_se_retoma(tmp_path):
    cfg = _settings(tmp_path, gracia_recepcion_s=0.05)
    with TestClient(create_app(cfg)) as c:
        lote = IngestionLote.model_validate(_lote())
        huérfano = c.app.state.registro.registrar(lote.request_id, huella_lote(lote))
        assert huérfano.accion == "nuevo"

        r = c.post(URL, json=_lote(), headers=H)
        assert r.status_code == 503 and r.json()["error"] == "recepcion_en_curso"
        assert list(cfg.raw_dir.iterdir()) == []

        time.sleep(0.1)
        r = c.post(URL, json=_lote(), headers=H)
        assert r.status_code == 202
        assert r.json()["run_id"] == huérfano.run_id
        assert [p.name for p in cfg.raw_dir.iterdir()] == [f"{huérfano.run_id}.json"]


def test_escritura_atomica_no_deja_temporales_ni_destino_si_falla(tmp_path, monkeypatch):
    lote = IngestionLote.model_validate(_lote())

    def falla(*_a, **_k):
        raise OSError("fallo en replace")

    monkeypatch.setattr(os, "replace", falla)
    with pytest.raises(OSError):
        escribir_lote_atomico(tmp_path, "run-x", lote)
    assert list(tmp_path.iterdir()) == []


def test_escritura_atomica_ignora_temporales_ajenos_y_deja_un_solo_archivo(tmp_path):
    lote = IngestionLote.model_validate(_lote())
    destino = escribir_lote_atomico(tmp_path, "run-x", lote)
    escribir_lote_atomico(tmp_path, "run-x", lote)
    assert [p.name for p in tmp_path.iterdir()] == [destino.name]


def test_get_run_requiere_secreto(cliente):
    assert cliente.get("/api/v1/runs/run-20260101T000000Z-aaaaaaaaaaaa").status_code == 401


def test_get_run_inexistente_o_con_formato_invalido_devuelve_404(cliente):
    assert (
        cliente.get("/api/v1/runs/run-20260101T000000Z-aaaaaaaaaaaa", headers=H).status_code == 404
    )
    assert cliente.get("/api/v1/runs/..%2F..%2Fetc", headers=H).status_code == 404


def test_get_run_devuelve_estado_y_sigue_al_runner(cliente):
    run_id = cliente.post(URL, json=_lote(), headers=H).json()["run_id"]
    r = cliente.get(f"/api/v1/runs/{run_id}", headers=H)
    assert r.status_code == 200
    assert r.json()["estado"] == "en_cola" and r.json()["request_id"] == "req-001"

    cliente.app.state.registro.actualizar_estado(run_id, "completado")
    assert cliente.get(f"/api/v1/runs/{run_id}", headers=H).json()["estado"] == "completado"


def test_health_no_requiere_autenticacion(cliente):
    r = cliente.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_los_logs_no_contienen_el_secreto_ni_pii(tmp_path, capsys):
    with TestClient(create_app(_settings(tmp_path))) as c:
        c.post(URL, json=_lote(request_id="req-ana@example.com"), headers=H)
        c.post(URL, json=_lote(request_id="req-2"), headers={"X-Webhook-Secret": "mal-secreto-x"})
    salida = capsys.readouterr().out
    assert "ingesta_recibida" in salida and "auth_fallida" in salida
    assert SECRETO not in salida and "mal-secreto-x" not in salida
    assert "ana@example.com" not in salida
    assert "Terminé mi primer proyecto" not in salida


def test_el_timestamp_del_log_no_se_redacta(tmp_path, capsys):
    with TestClient(create_app(_settings(tmp_path))):
        pass
    primera = json.loads(capsys.readouterr().out.splitlines()[0])
    datetime.fromisoformat(primera["timestamp"])


def test_redactar_enmascara_patrones_de_pii_y_preserva_ips():
    assert "ana@example.com" not in redactar("contacto ana@example.com")
    assert "+502 5555 1234" not in redactar("llamar al +502 5555 1234")
    assert "abc123" not in redactar("Authorization: Bearer abc123")
    assert redactar("run-20260101T000000Z-aaaaaaaaaaaa") == "run-20260101T000000Z-aaaaaaaaaaaa"
    # Mitigación Riesgo 5: No debe enmascarar direcciones IPv4 ni versiones en logs
    assert redactar("Host 10.0.10.245 v1.12.3-20261013") == "Host 10.0.10.245 v1.12.3-20261013"


def test_carrera_de_peticiones_identicas_crea_un_solo_run(tmp_path):
    reg = RegistroIdempotencia(tmp_path / "db.sqlite")
    reg.inicializar()
    resultados, barrera = [], threading.Barrier(16)

    def tarea():
        barrera.wait()
        resultados.append(reg.registrar("req-carrera", "a" * 64))

    hilos = [threading.Thread(target=tarea) for _ in range(16)]
    [h.start() for h in hilos]
    [h.join() for h in hilos]

    assert len(resultados) == 16
    assert len({r.run_id for r in resultados}) == 1
    assert sum(r.accion == "nuevo" for r in resultados) == 1


def test_carrera_con_contenidos_distintos_deja_un_ganador_y_409_al_resto(tmp_path):
    reg = RegistroIdempotencia(tmp_path / "db.sqlite")
    reg.inicializar()
    ok, conflictos, barrera = [], [], threading.Barrier(8)

    def tarea(i: int):
        barrera.wait()
        try:
            ok.append(reg.registrar("req-x", f"{i:064d}"))
        except ConflictoIdempotencia:
            conflictos.append(i)

    hilos = [threading.Thread(target=tarea, args=(i,)) for i in range(8)]
    [h.start() for h in hilos]
    [h.join() for h in hilos]
    assert len(ok) == 1 and len(conflictos) == 7


def test_retomar_es_compare_and_set_solo_un_reintento_gana(tmp_path):
    t0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    ahora = [t0]
    reg = RegistroIdempotencia(
        tmp_path / "db.sqlite", gracia_recepcion_s=30, reloj=lambda: ahora[0]
    )
    reg.inicializar()
    reg.registrar("req-1", "b" * 64)

    ahora[0] = t0 + timedelta(seconds=10)
    assert reg.registrar("req-1", "b" * 64).accion == "en_curso"

    ahora[0] = t0 + timedelta(seconds=45)
    assert reg.registrar("req-1", "b" * 64).accion == "retomar"
    assert reg.registrar("req-1", "b" * 64).accion == "en_curso"


def test_marcar_en_cola_no_degrada_un_estado_mas_avanzado(tmp_path):
    reg = RegistroIdempotencia(tmp_path / "db.sqlite")
    reg.inicializar()
    run_id = reg.registrar("req-1", "c" * 64).run_id
    assert reg.marcar_en_cola(run_id) is None
    reg.actualizar_estado(run_id, "procesando")
    reg.marcar_en_cola(run_id)  # llegada tardía
    assert reg.obtener_por_run_id(run_id).estado == "procesando"


def test_estado_desconocido_es_rechazado(tmp_path):
    reg = RegistroIdempotencia(tmp_path / "db.sqlite")
    reg.inicializar()
    run_id = reg.registrar("req-1", "c" * 64).run_id
    with pytest.raises(ValueError):
        reg.actualizar_estado(run_id, "inventado")


def test_descartar_recepcion_solo_borra_las_no_encoladas(tmp_path):
    reg = RegistroIdempotencia(tmp_path / "db.sqlite")
    reg.inicializar()
    a = reg.registrar("req-a", "d" * 64).run_id
    b = reg.registrar("req-b", "e" * 64).run_id
    reg.marcar_en_cola(b)
    reg.descartar_recepcion(a)
    reg.descartar_recepcion(b)
    assert reg.obtener_por_run_id(a) is None
    assert reg.obtener_por_run_id(b) is not None


def test_run_id_generado_es_seguro_para_rutas_oci():
    import re

    rid = generar_run_id(datetime(2026, 10, 17, 12, 0, tzinfo=UTC))
    assert re.fullmatch(r"[A-Za-z0-9._-]+", rid)
    assert re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{12}", rid)


def test_logging_no_queda_atado_a_un_stdout_cerrado(tmp_path, capsys):
    import io
    import sys

    import structlog

    from api.logging_config import configurar_logging

    configurar_logging("INFO")
    viejo = sys.stdout
    sys.stdout = io.StringIO()
    sys.stdout.close()
    sys.stdout = viejo
    structlog.get_logger("otro_modulo").info("sigue_funcionando")
    assert "sigue_funcionando" in capsys.readouterr().out