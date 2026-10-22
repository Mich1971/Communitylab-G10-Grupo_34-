"""Pruebas del worker local (orchestration/worker.py) con ejecutores simulados."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from api.main import escribir_lote_atomico, huella_lote
from api.registro import RegistroIdempotencia
from esquemas import IngestionLote
from orchestration.worker import WorkerLocal, WorkerYaEnEjecucion
from orchestration.worker import main as worker_main


def _lote(rid: str) -> IngestionLote:
    return IngestionLote.model_validate(
        {
            "request_id": rid,
            "origen_comunidad": "Discord",
            "periodo_referencia": "2026-W42",
            "interacciones": [
                {
                    "id": "m1",
                    "canal": "c",
                    "texto": "hola",
                    "metadata_origen": {
                        "plataforma": "Discord",
                        "identificador_original": "1",
                        "fecha": "2026-10-12T10:00:00Z",
                    },
                }
            ],
        }
    )


@pytest.fixture
def entorno(tmp_path):
    data = tmp_path / "data"
    registro = RegistroIdempotencia(data / "communitylab.db")
    registro.inicializar()
    return data, registro


def _depositar(data, registro, rid: str, encolar: bool = True) -> str:
    lote = _lote(rid)
    res = registro.registrar(rid, huella_lote(lote))
    escribir_lote_atomico(data / "raw", res.run_id, lote)
    if encolar:
        registro.marcar_en_cola(res.run_id)
    return res.run_id


def _ejecutor(estado: str, llamadas: list | None = None):
    async def f(ruta, run_id):
        if llamadas is not None:
            llamadas.append((ruta, run_id, json.loads(ruta.read_text())["request_id"]))
        return SimpleNamespace(estado_flujo=estado)

    return f


def _estado(registro, run_id):
    return registro.obtener_por_run_id(run_id).estado


def _worker(entorno, ejecutor):
    data, registro = entorno
    w = WorkerLocal(data, registro, ejecutor, intervalo_s=0.05)
    w.preparar()
    return w


@pytest.mark.parametrize(
    "estado", ["completado", "sin_registros_validos", "sin_contenido_publicable"]
)
def test_estado_de_negocio_se_registra_y_el_archivo_va_a_processed(entorno, estado):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    w = _worker(entorno, _ejecutor(estado))
    assert asyncio.run(w.ejecutar_una_pasada()) == 1
    assert _estado(registro, run_id) == estado
    assert (data / "processed" / f"{run_id}.json").exists()
    assert not list((data / "raw").iterdir())
    assert not list((data / "processing").iterdir())


def test_fallido_va_a_failed(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    asyncio.run(_worker(entorno, _ejecutor("fallido")).ejecutar_una_pasada())
    assert _estado(registro, run_id) == "fallido"
    assert (data / "failed" / f"{run_id}.json").exists()


def test_excepcion_del_ejecutor_no_tumba_el_worker_ni_pierde_el_archivo(entorno):
    data, registro = entorno
    malo = _depositar(data, registro, "malo")
    bueno = _depositar(data, registro, "bueno")

    async def ejecutor(ruta, run_id):
        if run_id == malo:
            raise RuntimeError("boom")
        return SimpleNamespace(estado_flujo="completado")

    asyncio.run(_worker(entorno, ejecutor).ejecutar_una_pasada())
    assert _estado(registro, malo) == "fallido"
    assert (data / "failed" / f"{malo}.json").exists()
    assert _estado(registro, bueno) == "completado"


def test_estado_flujo_invalido_se_trata_como_fallido(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    asyncio.run(_worker(entorno, _ejecutor("inventado")).ejecutar_una_pasada())
    assert _estado(registro, run_id) == "fallido"


def test_ignora_temporales_y_archivos_ajenos(entorno):
    data, _registro = entorno
    (data / "raw").mkdir(parents=True, exist_ok=True)
    (data / "raw" / "temp_run-20261018T000000Z-aaaaaaaaaaaa_x.json").write_text("{}")
    (data / "raw" / "notas.txt").write_text("x")
    llamadas: list = []
    assert asyncio.run(_worker(entorno, _ejecutor("completado", llamadas)).ejecutar_una_pasada()) == 0
    assert llamadas == []
    assert len(list((data / "raw").iterdir())) == 2  # intactos


def test_procesa_en_orden_cronologico_y_pasa_la_ruta_y_el_run_id(entorno):
    data, registro = entorno
    ids = [_depositar(data, registro, f"r{i}") for i in range(3)]
    llamadas: list = []
    asyncio.run(_worker(entorno, _ejecutor("completado", llamadas)).ejecutar_una_pasada())
    assert [c[1] for c in llamadas] == sorted(ids)
    assert all(c[0].parent.name == "processing" for c in llamadas)


def test_carrera_api_worker_el_estado_final_no_se_degrada(entorno):
    """El archivo ya está en raw/ pero la API aún no marcó `en_cola`."""
    data, registro = entorno
    run_id = _depositar(data, registro, "r1", encolar=False)
    assert _estado(registro, run_id) == "recibiendo"
    asyncio.run(_worker(entorno, _ejecutor("completado")).ejecutar_una_pasada())
    registro.marcar_en_cola(run_id)  # la API llega tarde
    assert _estado(registro, run_id) == "completado"


def test_recupera_huerfanos_tras_una_caida(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    w = _worker(entorno, _ejecutor("completado"))
    reclamo = w.reclamar_siguiente()
    registro.actualizar_estado(run_id, "procesando")  # el proceso murió aquí
    assert reclamo and not list((data / "raw").iterdir())

    w2 = WorkerLocal(data, registro, _ejecutor("completado"))
    assert w2.preparar() == 1
    assert _estado(registro, run_id) == "en_cola"
    assert asyncio.run(w2.ejecutar_una_pasada()) == 1
    assert _estado(registro, run_id) == "completado"


def test_reclamo_concurrente_solo_lo_gana_uno(entorno):
    data, registro = entorno
    _depositar(data, registro, "r1")
    a, b = _worker(entorno, _ejecutor("completado")), _worker(entorno, _ejecutor("completado"))
    assert (a.reclamar_siguiente() is None) != (b.reclamar_siguiente() is None)


def test_un_solo_worker_por_data_dir(entorno):
    data, registro = entorno
    a = WorkerLocal(data, registro, _ejecutor("completado"))
    b = WorkerLocal(data, registro, _ejecutor("completado"))
    a.adquirir_candado()
    with pytest.raises(WorkerYaEnEjecucion):
        b.adquirir_candado()
    a.liberar_candado()
    b.adquirir_candado()
    b.liberar_candado()


def test_correr_procesa_y_se_detiene_con_el_evento(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    w = WorkerLocal(data, registro, _ejecutor("completado"), intervalo_s=0.02)

    async def escenario():
        parar = asyncio.Event()
        tarea = asyncio.create_task(w.correr(parar))
        for _ in range(100):
            if _estado(registro, run_id) == "completado":
                break
            await asyncio.sleep(0.02)
        parar.set()
        await asyncio.wait_for(tarea, 2)

    asyncio.run(escenario())
    assert _estado(registro, run_id) == "completado"


def test_main_sin_configuracion_de_la_api_falla_rapido_con_mensaje_claro(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # sin `.env` a la vista
    monkeypatch.delenv("COMMUNITYLAB_WEBHOOK_SECRET", raising=False)

    with pytest.raises(SystemExit) as salida:
        worker_main()

    assert "COMMUNITYLAB_WEBHOOK_SECRET" in str(salida.value)
