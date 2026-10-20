"""Pruebas del orquestador `CommunityLabPipeline` (spec v3.1, Fase 4).

Todo corre sin red ni saldo: el analizador cognitivo, el generador de copys y
OCI se sustituyen por dobles. SQLite es real (archivo temporal).

Cubre un caso por cada estado terminal de la sección 5.3, el aislamiento de
fallos de OCI (M07), las invariantes de trazabilidad (sección 6.3), la
idempotencia por `run_id` y la integración con el worker.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

import orchestration.pipeline_runner as pr
from ai_modules.cognitive_analyzer import (
    TelemetriaAnalisis,
    _calcular_resumen_comunidad,
    _crear_mensaje_degradado,
)
from ai_modules.generador import TelemetriaGeneracion
from api.main import escribir_lote_atomico, huella_lote
from api.registro import RegistroIdempotencia
from esquemas import (
    AnalisisCognitivo,
    IngestionLote,
    MensajeAnalizado,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenSemanal,
)
from orchestration.pipeline_runner import (
    CommunityLabPipeline,
    calcular_enfoque_editorial,
    combinar_metadatos,
    invariante_casos,
    violaciones_trazabilidad,
)
from orchestration.worker import WorkerLocal
from storage.local_db import LocalDatabaseManager

RUN_ID = "run-20261020T040000Z-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# Datos y dobles
# ---------------------------------------------------------------------------


def _interaccion(id_: str, texto: str = "Terminé el módulo de Python y quiero compartirlo") -> dict:
    return {
        "id": id_,
        "autor": f"autor_{id_}",
        "canal": "general",
        "tipo": "mensaje",
        "texto": texto,
        "metadata_origen": {
            "plataforma": "Discord",
            "identificador_original": f"orig_{id_}",
            "fecha": "2026-10-12T10:00:00Z",
        },
    }


def _lote(textos: dict[str, str] | None = None, request_id: str = "req-001") -> IngestionLote:
    """Por defecto: 4 registros, 3 válidos (m1-m3) y 1 rechazado (m4, texto vacío)."""
    textos = textos or {"m1": "Logré desplegar mi primera API", "m2": "No entiendo los decoradores",
                        "m3": "Tuve un error con el entorno virtual", "m4": ""}
    return IngestionLote.model_validate(
        {
            "request_id": request_id,
            "origen_comunidad": "Discord",
            "periodo_referencia": "2026-W42",
            "interacciones": [_interaccion(i, t) for i, t in textos.items()],
        }
    )


def _mensaje(source_id: str, apto: bool, categoria: str = "Logro") -> MensajeAnalizado:
    return MensajeAnalizado(
        source_id=source_id,
        sentimiento="Positivo",
        categoria_enrutamiento=categoria,
        regla_aplicada="regla de prueba",
        temas=["python"],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2
        ),
        motivo_seleccion="Motivo objetivo de la puntuación",
        apto_para_publicacion=apto,
    )


def _telemetria(total: int, degradados: int = 0) -> TelemetriaAnalisis:
    return TelemetriaAnalisis(
        proveedor="fake", modelo="modelo-a", prompt_version="1.0.0+abc",
        latencia_ms=100, tokens_entrada=30, tokens_salida=20, tokens_cacheados=0,
        total_procesados=total, degradados=degradados,
    )


def _analisis(run_id, request_id, interacciones, mensajes) -> AnalisisCognitivo:
    return AnalisisCognitivo(
        run_id=run_id,
        request_id=request_id,
        resumen_comunidad=_calcular_resumen_comunidad(mensajes, len(interacciones)),
        mensajes_analizados=mensajes,
    )


def _analizador(reglas: dict[str, tuple[bool, str]] | None = None, degradar: bool = False,
                llamadas: list | None = None):
    """`reglas`: source_id -> (apto, categoría). Por defecto m1 apto, el resto no."""
    reglas = reglas or {"m1": (True, "Logro"), "m2": (False, "Duda"), "m3": (False, "Dificultad")}

    async def fake(interacciones, run_id, request_id, **_):
        if llamadas is not None:
            llamadas.append(run_id)
        if degradar:
            mensajes = [_crear_mensaje_degradado(i.id, RuntimeError("proveedor caído")) for i in interacciones]
            return (_analisis(run_id, request_id, interacciones, mensajes),
                    _telemetria(len(mensajes), degradados=len(mensajes)))
        mensajes = [_mensaje(i.id, *reglas.get(i.id, (False, "Duda"))) for i in interacciones]
        return (_analisis(run_id, request_id, interacciones, mensajes), _telemetria(len(mensajes)))

    return fake


def _generador(llamadas: list | None = None, post_ids=None, resumen_ids=None):
    """Genera ambos formatos; por defecto cita los aptos (post) y todos (resumen)."""

    async def fake(analisis, interacciones, enfoque, run_id, request_id, temperatura_cero=False, **_):
        if llamadas is not None:
            llamadas.append({"enfoque": enfoque, "temperatura_cero": temperatura_cero})
        aptos = [m.source_id for m in analisis.mensajes_analizados if m.apto_para_publicacion]
        todos = [m.source_id for m in analisis.mensajes_analizados]
        intento = len(llamadas) if llamadas is not None else 1
        p_ids = post_ids(intento) if callable(post_ids) else (post_ids or aptos)
        r_ids = resumen_ids(intento) if callable(resumen_ids) else (resumen_ids or todos)
        post = PostLinkedIn(titulo="Logros de la semana", cuerpo="La comunidad compartió avances reales.",
                            hashtags=["#python"], source_ids=p_ids)
        resumen = ResumenSemanal(seccion="Destacados", titular="Semana de aprendizaje",
                                 resumen="Resumen basado en mensajes reales de la comunidad.",
                                 source_ids=r_ids)
        tel = TelemetriaGeneracion(proveedor="fake", modelo="modelo-b", prompt_version="gen-1",
                                   latencia_ms=50, tokens_entrada=10, tokens_salida=10)
        return post, resumen, tel

    return fake


class FakeOCI:
    def __init__(self, status="guardado_con_exito", lectura=True, falla: Exception | None = None):
        self.config = SimpleNamespace(bucket_name="bucket-test")
        self.status, self.lectura, self.falla = status, lectura, falla
        self.subidas: list[dict] = []

    def build_object_path(self, periodo, run_id, numero_revision):
        return f"activos/{periodo}/{run_id}/revision-{numero_revision:03d}.json"

    def upload_package(self, package_payload, periodo_referencia, run_id, numero_revision=1,
                       request_id="req-desconocido"):
        self.subidas.append({"payload": package_payload, "run_id": run_id, "request_id": request_id})
        if self.falla:
            raise self.falla
        return SimpleNamespace(
            bucket="bucket-test",
            ruta_objeto=self.build_object_path(periodo_referencia, run_id, numero_revision),
            status_almacenamiento=self.status,
            comprobacion_lectura=self.lectura,
        )


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "datos" / "communitylab.db"


@pytest.fixture
def oci():
    return FakeOCI()


@pytest.fixture
def pipeline(db_path, oci, monkeypatch):
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador())
    return CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))


def _consulta(db_path, sql, *params):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _ejecutar(pipeline, lote=None, run_id=RUN_ID):
    return asyncio.run(pipeline.ejecutar_desde_lote(lote or _lote(), run_id))


# ---------------------------------------------------------------------------
# Estados terminales (sección 5.3)
# ---------------------------------------------------------------------------


def test_completado_lote_de_4_registros_3_validos_1_rechazado(pipeline, db_path, oci):
    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "completado"
    assert resultado.run_id == RUN_ID and resultado.request_id == "req-001"
    paquete = resultado.paquete_salida
    assert paquete is not None
    assert paquete.revision.numero_revision == 1 and paquete.revision.estado == "pendiente"
    # M01: siempre ambos formatos.
    assert paquete.activos.post_linkedin.source_ids == ["m1"]
    assert paquete.activos.resumen_semanal.source_ids == ["m1", "m2", "m3"]
    # Casos no publicables: exactamente los no aptos, construidos de forma determinista.
    assert sorted(c.source_id for c in resultado.casos_no_publicables) == ["m2", "m3"]
    assert sorted(c.source_id for c in paquete.activos.casos_no_publicables) == ["m2", "m3"]
    # El rechazado del validador viaja en el resultado.
    assert [r.mensaje_id for r in resultado.registros_rechazados] == ["m4"]
    # OCI con comprobación de lectura inyectada en el paquete.
    assert paquete.almacenamiento_oci.status_almacenamiento == "guardado_con_exito"
    assert paquete.almacenamiento_oci.comprobacion_lectura is True
    assert oci.subidas[0]["request_id"] == "req-001"
    # Metadatos: análisis + copys (sección 8, punto 11).
    assert paquete.metadatos_ejecucion.modelo == "modelo-a + modelo-b"
    assert paquete.metadatos_ejecucion.latencia_ms == 150
    assert paquete.metadatos_ejecucion.tokens_totales == 70

    # SQLite
    assert _consulta(db_path, "SELECT total_recibidos, total_validos, total_rechazados, estado_flujo "
                              "FROM lotes_procesamiento WHERE run_id=?", RUN_ID) == [(4, 3, 1, "completado")]
    assert _consulta(db_path, "SELECT COUNT(*) FROM auditoria_rechazados WHERE run_id=?", RUN_ID) == [(1,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM mensajes_analizados WHERE run_id=?", RUN_ID) == [(3,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM mensaje_temas") == [(3,)]
    assert _consulta(db_path, "SELECT estado FROM estado_revision WHERE run_id=?", RUN_ID) == [("pendiente",)]
    assert _consulta(db_path, "SELECT status_almacenamiento, comprobacion_lectura FROM "
                              "evidencia_almacenamiento_oci WHERE run_id=?", RUN_ID) == [("guardado_con_exito", 1)]


def test_completado_con_guardado_error_si_oci_devuelve_fallo(db_path, monkeypatch):
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador())
    oci = FakeOCI(status="guardado_error", lectura=False)
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "completado"  # M07: OCI no aborta el flujo
    evidencia = resultado.paquete_salida.almacenamiento_oci
    assert evidencia.status_almacenamiento == "guardado_error" and evidencia.comprobacion_lectura is False
    # Paquete conservado en SQLite y en disco para `retry_upload_from_disk()`.
    assert _consulta(db_path, "SELECT status_almacenamiento FROM evidencia_almacenamiento_oci "
                              "WHERE run_id=?", RUN_ID) == [("guardado_error",)]
    copia = db_path.parent / "pending_oci" / f"{RUN_ID}-revision-001.json"
    assert json.loads(copia.read_text(encoding="utf-8"))["run_id"] == RUN_ID


def test_completado_con_guardado_error_si_oci_lanza_excepcion(db_path, monkeypatch):
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador())
    oci = FakeOCI(falla=ConnectionError("sin red"))
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "completado"
    assert resultado.paquete_salida.almacenamiento_oci.status_almacenamiento == "guardado_error"


def test_sin_registros_validos_no_invoca_llm_ni_oci(db_path, oci, monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador(llamadas=llamadas))
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador(llamadas))
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline, _lote({"m1": "", "m2": "   "}))

    assert resultado.estado_flujo == "sin_registros_validos"
    assert resultado.paquete_salida is None
    assert len(resultado.registros_rechazados) == 2
    assert resultado.errores  # motivo registrado
    assert llamadas == [] and oci.subidas == []
    assert _consulta(db_path, "SELECT total_validos, total_rechazados, estado_flujo FROM lotes_procesamiento "
                              "WHERE run_id=?", RUN_ID) == [(0, 2, "sin_registros_validos")]
    assert _consulta(db_path, "SELECT COUNT(*) FROM auditoria_rechazados WHERE run_id=?", RUN_ID) == [(2,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM paquetes_salida") == [(0,)]


def test_sin_contenido_publicable_persiste_analisis_pero_no_paquete(db_path, oci, monkeypatch):
    llamadas: list = []
    reglas = {"m1": (False, "Duda"), "m2": (False, "Duda"), "m3": (False, "Dificultad")}
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador(reglas))
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador(llamadas))
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "sin_contenido_publicable"
    assert resultado.paquete_salida is None
    assert sorted(c.source_id for c in resultado.casos_no_publicables) == ["m1", "m2", "m3"]
    assert llamadas == [] and oci.subidas == []
    assert _consulta(db_path, "SELECT COUNT(*) FROM mensajes_analizados WHERE run_id=?", RUN_ID) == [(3,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM paquetes_salida") == [(0,)]


def test_fallido_si_todos_los_mensajes_fueron_degradados(db_path, oci, monkeypatch):
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador(degradar=True))
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador())
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "fallido"
    assert resultado.paquete_salida is None
    assert any(e.startswith("inferencia no disponible") for e in resultado.errores)
    assert len(resultado.casos_no_publicables) == 3  # el curador puede reintentarlos
    assert oci.subidas == []


def test_fallido_si_una_excepcion_no_controlada_interrumpe_el_grafo(db_path, oci, monkeypatch):
    async def explota(*_a, **_k):
        raise RuntimeError("fallo inesperado del analizador")

    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", explota)
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)  # nunca lanza: lo devuelve como `fallido`

    assert resultado.estado_flujo == "fallido"
    assert resultado.paquete_salida is None
    assert "fallo inesperado del analizador" in resultado.errores[0]
    # Los rechazos ya estaban en SQLite (Nodo 1) y el manejador los relee por run_id.
    assert [r.mensaje_id for r in resultado.registros_rechazados] == ["m4"]


# ---------------------------------------------------------------------------
# Invariantes de trazabilidad (sección 6.3)
# ---------------------------------------------------------------------------


def test_post_que_cita_un_mensaje_no_apto_se_reintenta_con_temperatura_cero(db_path, oci, monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    # Primer intento: cita m2 (no apto). Segundo: correcto.
    monkeypatch.setattr(
        pr, "generar_activos_marketing",
        _generador(llamadas, post_ids=lambda intento: ["m2"] if intento == 1 else ["m1"]),
    )
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "completado"
    assert [c["temperatura_cero"] for c in llamadas] == [False, True]
    assert resultado.paquete_salida.activos.post_linkedin.source_ids == ["m1"]
    # La telemetría acumula ambos intentos.
    assert resultado.paquete_salida.metadatos_ejecucion.latencia_ms == 100 + 50 * 2


def test_generador_que_siempre_inventa_ids_termina_en_fallido(db_path, oci, monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador(llamadas, resumen_ids=["fantasma"]))
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert resultado.estado_flujo == "fallido"
    assert resultado.paquete_salida is None
    assert len(llamadas) == pr.MAX_REINTENTOS_INVARIANTES + 1
    assert "trazabilidad" in resultado.errores[0]
    assert oci.subidas == []


def test_resumen_semanal_puede_citar_mensajes_no_aptos_pero_post_no():
    analisis = _analisis("r", "q", [SimpleNamespace(id=i) for i in ("m1", "m2")],
                         [_mensaje("m1", True), _mensaje("m2", False)])
    post_ok = PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 25, source_ids=["m1"])
    post_mal = PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 25, source_ids=["m2"])
    resumen = ResumenSemanal(seccion="s", titular="Titular ok", resumen="r" * 25, source_ids=["m1", "m2"])

    assert violaciones_trazabilidad(analisis, post_ok, resumen) == []
    assert violaciones_trazabilidad(analisis, post_mal, resumen)
    resumen_mal = ResumenSemanal(seccion="s", titular="Titular ok", resumen="r" * 25, source_ids=["zzz"])
    assert violaciones_trazabilidad(analisis, post_ok, resumen_mal)


def test_invariante_casos_exige_el_conjunto_exacto_sin_repetidos():
    analisis = _analisis("r", "q", [SimpleNamespace(id=i) for i in ("m1", "m2", "m3")],
                         [_mensaje("m1", True), _mensaje("m2", False), _mensaje("m3", False)])
    from esquemas import CasoNoPublicable

    casos = [CasoNoPublicable.desde_mensaje_analizado(m)
             for m in analisis.mensajes_analizados if not m.apto_para_publicacion]
    assert invariante_casos(analisis, casos)
    assert not invariante_casos(analisis, casos[:1])  # falta uno
    assert not invariante_casos(analisis, casos + casos[:1])  # duplicado


# ---------------------------------------------------------------------------
# Enfoque editorial (M01) y metadatos
# ---------------------------------------------------------------------------


def _analisis_con_categorias(categorias: list[str]) -> AnalisisCognitivo:
    mensajes = [_mensaje(f"m{i}", True, c) for i, c in enumerate(categorias)]
    return _analisis("r", "q", mensajes, mensajes)


def test_enfoque_editorial_mayoritario_y_prioridad_en_empate():
    assert calcular_enfoque_editorial(_analisis_con_categorias(["Duda", "Duda", "Logro"])) == (
        "Duda", {"Logro": 1, "Duda": 2, "Dificultad": 0})
    assert calcular_enfoque_editorial(_analisis_con_categorias(["Duda", "Dificultad", "Logro"]))[0] == "Logro"
    assert calcular_enfoque_editorial(_analisis_con_categorias(["Duda", "Dificultad"]))[0] == "Dificultad"


def test_enfoque_duda_no_bifurca_el_formato_se_generan_ambos(db_path, oci, monkeypatch):
    llamadas: list = []
    reglas = {"m1": (True, "Duda"), "m2": (True, "Duda"), "m3": (False, "Dificultad")}
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador(reglas))
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador(llamadas))
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(db_path))

    resultado = _ejecutar(pipeline)

    assert llamadas[0]["enfoque"] == "Duda"
    assert resultado.paquete_salida.activos.post_linkedin and resultado.paquete_salida.activos.resumen_semanal


def test_combinar_metadatos_suma_latencia_y_tokens():
    meta = combinar_metadatos(
        _telemetria(2),
        TelemetriaGeneracion(proveedor="p", modelo="gpt-x", prompt_version="g1", latencia_ms=7,
                             tokens_entrada=1, tokens_salida=2),
    )
    assert (meta.modelo, meta.prompt_version) == ("modelo-a + gpt-x", "1.0.0+abc + g1")
    assert (meta.latencia_ms, meta.tokens_totales) == (107, 53)
    assert combinar_metadatos(_telemetria(2), None).modelo == "modelo-a"


# ---------------------------------------------------------------------------
# Entrada desde archivo (Nodo 1, pasos 1-2)
# ---------------------------------------------------------------------------


def test_ejecutar_desde_archivo_lee_el_json_dentro_del_nodo(pipeline, tmp_path):
    ruta = tmp_path / f"{RUN_ID}.json"
    ruta.write_text(_lote().model_dump_json(), encoding="utf-8")

    resultado = asyncio.run(pipeline.ejecutar_desde_archivo(ruta, RUN_ID))

    assert resultado.estado_flujo == "completado"
    assert resultado.request_id == "req-001"


def test_ejecutar_desde_archivo_deduce_el_run_id_del_nombre_o_lo_genera(pipeline, tmp_path):
    ruta = tmp_path / f"{RUN_ID}.json"
    ruta.write_text(_lote().model_dump_json(), encoding="utf-8")
    assert asyncio.run(pipeline.ejecutar_desde_archivo(ruta)).run_id == RUN_ID

    manual = tmp_path / "lote_prueba_01.json"
    manual.write_text(_lote(request_id="req-manual").model_dump_json(), encoding="utf-8")
    generado = asyncio.run(pipeline.ejecutar_desde_archivo(str(manual))).run_id
    assert generado.startswith("run-") and generado != RUN_ID


def test_archivo_en_formato_plano_del_jurado_se_adapta(pipeline, tmp_path):
    plano = {
        "origen_comunidad": "Discord_general",
        "periodo_referencia": "2026-W42",
        "request_id": "req-plano",
        "interacciones": [
            {"autor": "ana", "canal": "general", "texto": "Logré desplegar mi primera API",
             "metadata_origen": {"fecha": "2026-10-12T10:00:00Z"}},
        ],
    }
    ruta = tmp_path / "plano.json"
    ruta.write_text(json.dumps(plano), encoding="utf-8")

    resultado = asyncio.run(pipeline.ejecutar_desde_archivo(ruta, RUN_ID))

    assert resultado.estado_flujo in {"completado", "sin_contenido_publicable"}
    assert resultado.request_id == "req-plano"


def test_archivo_inexistente_desvia_a_sin_registros_validos(pipeline, tmp_path):
    resultado = asyncio.run(pipeline.ejecutar_desde_archivo(tmp_path / "no_existe.json", RUN_ID))
    assert resultado.estado_flujo == "sin_registros_validos"
    assert "no encontrado" in resultado.errores[0]
    assert resultado.paquete_salida is None


def test_json_mal_formado_desvia_a_sin_registros_validos(pipeline, tmp_path):
    ruta = tmp_path / "roto.json"
    ruta.write_text("{ esto no es json", encoding="utf-8")
    resultado = asyncio.run(pipeline.ejecutar_desde_archivo(ruta, RUN_ID))
    assert resultado.estado_flujo == "sin_registros_validos"
    assert "JSON mal formado" in resultado.errores[0]


# ---------------------------------------------------------------------------
# Idempotencia por run_id (sección 5.4)
# ---------------------------------------------------------------------------


def test_reejecutar_el_mismo_run_id_no_duplica_filas(pipeline, db_path):
    primero = _ejecutar(pipeline)
    segundo = _ejecutar(pipeline)

    assert primero.estado_flujo == segundo.estado_flujo == "completado"
    assert _consulta(db_path, "SELECT COUNT(*) FROM lotes_procesamiento") == [(1,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM mensajes_analizados") == [(3,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM mensaje_temas") == [(3,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM auditoria_rechazados") == [(1,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM paquetes_salida") == [(1,)]
    assert _consulta(db_path, "SELECT COUNT(*) FROM post_linkedin_source_ids") == [(1,)]


def test_paquete_persistido_es_reconstruible_con_las_funciones_de_local_db(pipeline, db_path, monkeypatch):
    from storage import local_db

    _ejecutar(pipeline)
    monkeypatch.setattr(local_db, "DB_PATH", str(db_path))

    reconstruido = local_db.reconstruir_json_paquete_salida(RUN_ID, 1)

    assert reconstruido["activos_distribucion_generados"]["post_linkedin"]["titulo"] == "Logros de la semana"
    assert reconstruido["almacenamiento_oci"]["status"] == "guardado_con_exito"


# ---------------------------------------------------------------------------
# Integración con el worker (sección 5.4)
# ---------------------------------------------------------------------------


def test_worker_ejecuta_el_pipeline_y_refleja_el_estado_en_el_registro(tmp_path, oci, monkeypatch):
    monkeypatch.setattr(pr, "analizar_lote_cognitivo_con_telemetria", _analizador())
    monkeypatch.setattr(pr, "generar_activos_marketing", _generador())
    data = tmp_path / "data"
    registro = RegistroIdempotencia(data / "communitylab.db")
    registro.inicializar()
    pipeline = CommunityLabPipeline(oci_client=oci, db_manager=LocalDatabaseManager(data / "communitylab.db"))

    lote = _lote()
    res = registro.registrar(lote.request_id, huella_lote(lote))
    escribir_lote_atomico(data / "raw", res.run_id, lote)
    registro.marcar_en_cola(res.run_id)

    worker = WorkerLocal(data, registro, pipeline.ejecutar_desde_archivo, intervalo_s=0.05)
    worker.preparar()
    assert asyncio.run(worker.ejecutar_una_pasada()) == 1

    assert registro.obtener_por_run_id(res.run_id).estado == "completado"
    assert (data / "processed" / f"{res.run_id}.json").exists()
    assert _consulta(data / "communitylab.db", "SELECT COUNT(*) FROM paquetes_salida WHERE run_id=?",
                     res.run_id) == [(1,)]
