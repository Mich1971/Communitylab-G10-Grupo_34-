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


# --- reconciliación, tope de intentos y errores de E/S -----------------------


def test_caida_entre_registrar_el_estado_y_archivar_se_resuelve_sin_reejecutar(entorno, monkeypatch):
    """Antes: el archivo iba a processed/ y el estado quedaba en `procesando` para siempre."""
    import orchestration.worker as W

    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    llamadas: list = []
    w = _worker(entorno, _ejecutor("completado", llamadas))

    real = W.os.replace

    def replace_que_cae_al_archivar(origen, destino):
        if "processed" in str(destino):
            raise OSError("caída simulada al archivar")
        return real(origen, destino)

    monkeypatch.setattr(W.WorkerLocal, "_ESPERA_ARCHIVADO_S", 0.0)
    monkeypatch.setattr(W.os, "replace", replace_que_cae_al_archivar)
    asyncio.run(w.ejecutar_una_pasada())  # el fallo al archivar ya no tumba al worker
    monkeypatch.setattr(W.os, "replace", real)

    assert _estado(registro, run_id) == "completado"  # el resultado ya estaba registrado
    assert (data / "processing" / f"{run_id}.json").exists()

    w2 = WorkerLocal(data, registro, _ejecutor("completado", llamadas))
    assert w2.preparar() == 0  # nada que reencolar
    assert (data / "processed" / f"{run_id}.json").exists()
    assert not list((data / "processing").iterdir())
    assert len(llamadas) == 1  # NO se reejecutó
    assert _estado(registro, run_id) == "completado"


def test_caida_tras_un_fallido_archiva_en_failed(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    w = _worker(entorno, _ejecutor("fallido"))
    w.reclamar_siguiente()
    registro.actualizar_estado(run_id, "fallido")  # registrado, pero el archivo quedó en processing/
    assert WorkerLocal(data, registro, _ejecutor("fallido")).preparar() == 0
    assert (data / "failed" / f"{run_id}.json").exists()


def test_un_lote_que_mata_el_proceso_deja_de_reencolarse_tras_el_maximo_de_intentos(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    llamadas: list = []
    for _ in range(2):  # dos intentos que terminan con la muerte del proceso
        w = WorkerLocal(data, registro, _ejecutor("completado", llamadas), max_intentos=2)
        assert w.preparar() in (0, 1)
        assert w.reclamar_siguiente() is not None
        registro.registrar_intento(run_id)
        registro.actualizar_estado(run_id, "procesando")  # ...y el proceso muere aquí

    w = WorkerLocal(data, registro, _ejecutor("completado", llamadas), max_intentos=2)
    assert w.preparar() == 1  # vuelve a raw/
    assert asyncio.run(w.ejecutar_una_pasada()) == 1
    assert llamadas == []  # el tercer intento ya no ejecuta
    assert _estado(registro, run_id) == "fallido"
    assert (data / "failed" / f"{run_id}.json").exists()
    assert registro.obtener_por_run_id(run_id).intentos == 3


def test_una_ejecucion_normal_cuenta_un_intento(entorno):
    data, registro = entorno
    run_id = _depositar(data, registro, "r1")
    asyncio.run(_worker(entorno, _ejecutor("completado")).ejecutar_una_pasada())
    assert registro.obtener_por_run_id(run_id).intentos == 1


def test_lote_manual_sin_registro_se_ejecuta_sin_tope_de_intentos(entorno):
    data, _registro = entorno
    (data / "raw").mkdir(parents=True, exist_ok=True)
    manual = "run-20261018T000000Z-aaaaaaaaaaaa"
    (data / "raw" / f"{manual}.json").write_text('{"request_id": "manual"}')
    llamadas: list = []
    assert asyncio.run(_worker(entorno, _ejecutor("completado", llamadas)).ejecutar_una_pasada()) == 1
    assert llamadas and (data / "processed" / f"{manual}.json").exists()


def test_error_de_es_al_reclamar_se_salta_el_archivo_y_no_tumba_al_worker(entorno, monkeypatch):
    import orchestration.worker as W

    data, registro = entorno
    bloqueado = _depositar(data, registro, "bloqueado")
    libre = _depositar(data, registro, "libre")
    primero, segundo = sorted([bloqueado, libre])
    w = _worker(entorno, _ejecutor("completado"))

    real = W.os.replace

    def replace(origen, destino):
        if str(origen).endswith(f"{primero}.json"):
            raise PermissionError("bloqueado por otro proceso")
        return real(origen, destino)

    monkeypatch.setattr(W.os, "replace", replace)
    reclamo = w.reclamar_siguiente()  # no lanza
    assert reclamo is not None and reclamo.run_id == segundo
    assert (data / "raw" / f"{primero}.json").exists()  # queda para la próxima pasada


# --- reconciliación con la versión de Enrique (Windows, config, cargador, archivado) ----------

import sys

import orchestration.worker as W


class _MsvcrtFalso:
    """Imita `msvcrt`: el primer bloqueo del byte 0 gana; escribir en el archivo bloqueado falla."""

    LK_NBLCK, LK_UNLCK = 1, 0
    bloqueado: set = set()  # noqa: RUF012

    @classmethod
    def locking(cls, fd, modo, n):
        if modo == cls.LK_NBLCK:
            if "lock" in cls.bloqueado:
                raise OSError("bloqueado")
            cls.bloqueado.add("lock")
        else:
            cls.bloqueado.discard("lock")


def test_candado_de_windows_no_escribe_antes_de_bloquear_y_el_segundo_worker_recibe_el_error_correcto(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(W, "fcntl", None)
    monkeypatch.setattr(W, "msvcrt", _MsvcrtFalso)
    _MsvcrtFalso.bloqueado = set()
    registro = RegistroIdempotencia(tmp_path / "d" / "c.db")
    registro.inicializar()
    w1 = WorkerLocal(tmp_path / "d", registro, _ejecutor("completado"))
    w2 = WorkerLocal(tmp_path / "d", registro, _ejecutor("completado"))
    w1.adquirir_candado()
    assert (tmp_path / "d" / "worker.lock").read_text() == ""  # nada escrito: en Windows fallaría
    with pytest.raises(WorkerYaEnEjecucion):
        w2.adquirir_candado()
    w1.liberar_candado()
    w2.adquirir_candado()  # tras liberar, el segundo ya puede
    w2.liberar_candado()


def test_main_con_configuracion_invalida_da_un_mensaje_claro(monkeypatch, tmp_path):
    monkeypatch.delenv("COMMUNITYLAB_WEBHOOK_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)  # sin .env
    with pytest.raises(SystemExit) as e:
        W.main()
    assert "COMMUNITYLAB_WEBHOOK_SECRET" in str(e.value) and "Configuración inválida" in str(e.value)


def test_cargador_distingue_modulo_ausente_de_un_import_roto_dentro_del_modulo(tmp_path, monkeypatch):
    import orchestration

    monkeypatch.delitem(sys.modules, "orchestration.pipeline_runner", raising=False)
    with pytest.raises(SystemExit, match="aún no existe"):
        if (tmp_path / "pipeline_runner.py").exists():  # pragma: no cover
            pass
        # Ausente de verdad solo si el repo todavía no lo trae; si existe, se omite.
        import importlib.util

        if importlib.util.find_spec("orchestration.pipeline_runner") is not None:
            pytest.skip("pipeline_runner ya existe en este repositorio")
        W._cargar_ejecutor_por_defecto()

    # Existe pero falla por una dependencia interna: el error real debe verse, no un SystemExit engañoso.
    (tmp_path / "pipeline_runner.py").write_text("import dependencia_que_no_existe_xyz\n")
    monkeypatch.setattr(orchestration, "__path__", [*orchestration.__path__, str(tmp_path)])
    with pytest.raises(ModuleNotFoundError, match="dependencia_que_no_existe_xyz"):
        W._cargar_ejecutor_por_defecto()


def test_acepta_un_ejecutor_sincrono(entorno):
    rid = _depositar(*entorno, "req-sync")

    def sincrono(ruta, run_id):
        return SimpleNamespace(estado_flujo="completado")

    w = _worker(entorno, sincrono)
    assert asyncio.run(w.ejecutar_una_pasada()) == 1
    assert _estado(entorno[1], rid) == "completado"


def test_un_bloqueo_transitorio_al_archivar_se_reintenta(entorno, monkeypatch):
    rid = _depositar(*entorno, "req-bloq")
    w = _worker(entorno, _ejecutor("completado"))
    monkeypatch.setattr(W.WorkerLocal, "_ESPERA_ARCHIVADO_S", 0.0)
    real = W.os.replace
    fallos = {"n": 0}

    def inestable(a, b):
        if "processed" in str(b) and fallos["n"] < 2:
            fallos["n"] += 1
            raise PermissionError("archivo en uso")
        return real(a, b)

    monkeypatch.setattr(W.os, "replace", inestable)
    assert asyncio.run(w.ejecutar_una_pasada()) == 1
    assert fallos["n"] == 2 and _estado(entorno[1], rid) == "completado"
    assert (entorno[0] / "processed").glob("*.json") and not list((entorno[0] / "processing").glob("*.json"))


def test_un_bloqueo_persistente_al_archivar_no_tumba_al_worker_y_se_resuelve_al_reiniciar(entorno, monkeypatch):
    data, registro = entorno
    rid = _depositar(data, registro, "req-persist")
    w = _worker(entorno, _ejecutor("completado"))
    monkeypatch.setattr(W.WorkerLocal, "_ESPERA_ARCHIVADO_S", 0.0)
    real = W.os.replace

    def siempre(a, b):
        if "processed" in str(b):
            raise PermissionError("archivo en uso")
        return real(a, b)

    monkeypatch.setattr(W.os, "replace", siempre)
    assert asyncio.run(w.ejecutar_una_pasada()) == 1  # no lanza
    assert _estado(registro, rid) == "completado"  # el estado terminal ya quedó registrado
    assert len(list((data / "processing").glob("*.json"))) == 1  # el archivo espera al reinicio

    monkeypatch.setattr(W.os, "replace", real)
    w2 = WorkerLocal(data, registro, _ejecutor("completado"))
    w2.preparar()  # termina el archivado SIN reejecutar
    assert len(list((data / "processed").glob("*.json"))) == 1
    assert not list((data / "processing").glob("*.json"))


# --- integración con pipeline_runner (carga, base compartida, preflight, log de fallos) ----------


def _modulo_runner_falso(monkeypatch, metodos_db, clase_db_falla=False):
    """Inyecta un `orchestration.pipeline_runner` con la forma de la spec 5.2."""
    import types

    class _DB:
        pass

    for m in metodos_db:
        setattr(_DB, m, lambda self, *a, **k: None)

    class CommunityLabPipeline:
        recibido = None

        def __init__(self, oci_client=None, db_manager=None):
            if clase_db_falla:
                raise RuntimeError("OCI_NAMESPACE no definido")
            CommunityLabPipeline.recibido = db_manager
            self.db_manager = db_manager if db_manager is not None else _DB()

        async def ejecutar_desde_archivo(self, ruta, run_id=None):
            return SimpleNamespace(estado_flujo="completado")

    mod = types.ModuleType("orchestration.pipeline_runner")
    mod.CommunityLabPipeline = CommunityLabPipeline
    monkeypatch.setitem(sys.modules, "orchestration.pipeline_runner", mod)
    return CommunityLabPipeline


def test_el_cargador_pasa_al_orquestador_la_misma_base_que_usa_el_registro(tmp_path, monkeypatch):
    clase = _modulo_runner_falso(monkeypatch, W._METODOS_DB_REQUERIDOS)
    # El manager real no trae todavía los métodos del orquestador; se completan para esta prueba.
    from storage import local_db as L

    for m in W._METODOS_DB_REQUERIDOS:
        if not hasattr(L.LocalDatabaseManager, m):
            monkeypatch.setattr(L.LocalDatabaseManager, m, lambda self, *a, **k: None, raising=False)
    db = tmp_path / "datos" / "communitylab.db"
    ejecutor = W._cargar_ejecutor_por_defecto(db)
    assert clase.recibido.db_path == str(db)  # misma base que la API, sin depender del entorno
    assert asyncio.run(ejecutor(tmp_path / "x.json", "run-x")).estado_flujo == "completado"


def test_el_worker_no_arranca_si_la_base_no_implementa_lo_que_usa_el_orquestador(monkeypatch):
    _modulo_runner_falso(monkeypatch, ("registrar_lote", "registrar_paquete"))
    with pytest.raises(SystemExit) as e:
        W._cargar_ejecutor_por_defecto()
    texto = str(e.value)
    assert "registrar_lote_y_analisis" in texto and "leer_rechazados" in texto and "actualizar_estado_flujo" in texto
    assert "registrar_lote," not in texto  # solo lista lo que falta


def test_el_cargador_acepta_una_base_completa(monkeypatch):
    _modulo_runner_falso(monkeypatch, W._METODOS_DB_REQUERIDOS)
    assert callable(W._cargar_ejecutor_por_defecto())


def test_un_fallo_al_construir_el_orquestador_da_un_mensaje_claro(monkeypatch):
    _modulo_runner_falso(monkeypatch, W._METODOS_DB_REQUERIDOS, clase_db_falla=True)
    with pytest.raises(SystemExit, match="RuntimeError.*OCI_NAMESPACE"):
        W._cargar_ejecutor_por_defecto()


def test_un_run_fallido_deja_en_el_log_el_motivo_que_reporto_el_pipeline(entorno):
    from structlog.testing import capture_logs

    rid = _depositar(*entorno, "req-motivo")

    async def ejecutor(ruta, run_id):
        return SimpleNamespace(estado_flujo="fallido", errores=["AttributeError: sin registrar_lote", "x" * 500])

    w = _worker(entorno, ejecutor)
    with capture_logs() as eventos:
        asyncio.run(w.ejecutar_una_pasada())
    ev = next(e for e in eventos if e["event"] == "run_fallido_por_el_pipeline")
    assert ev["errores"][0].startswith("AttributeError") and len(ev["errores"][1]) == 300
    assert _estado(entorno[1], rid) == "fallido"
