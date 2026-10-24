"""Telemetría por etapa: el orquestador debe persistir `telemetria_llamadas_llm`.

Se ejecuta el `CommunityLabPipeline` real con base SQLite real y cliente OCI real sobre un
backend en memoria. Solo se simulan las llamadas al LLM (analizador y generador).
Si `ai_modules/generador.py` aún no está en el entorno, se instala un sustituto mínimo.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from dataclasses import dataclass

import pytest
import structlog

try:  # pragma: no cover - depende de si el módulo de Roberto ya está integrado
    import ai_modules.generador
except ImportError:  # pragma: no cover
    from esquemas import MetadatosEjecucion

    @dataclass
    class TelemetriaGeneracion:
        proveedor: str
        modelo: str
        prompt_version: str
        latencia_ms: int
        tokens_entrada: int
        tokens_salida: int
        tokens_cacheados: int = 0

        @property
        def tokens_totales(self) -> int:
            return self.tokens_entrada + self.tokens_salida

        def a_metadatos_ejecucion(self) -> MetadatosEjecucion:
            return MetadatosEjecucion(
                modelo=self.modelo, prompt_version=self.prompt_version,
                latencia_ms=self.latencia_ms, tokens_totales=self.tokens_totales,
            )

    _stub = types.ModuleType("ai_modules.generador")
    _stub.TelemetriaGeneracion = TelemetriaGeneracion
    _stub.generar_activos_marketing = None  # se sustituye en cada prueba
    sys.modules["ai_modules.generador"] = _stub

from ai_modules.generador import TelemetriaGeneracion

from ai_modules.cognitive_analyzer import TelemetriaAnalisis
from esquemas import PostLinkedIn, ResumenSemanal
from orchestration import pipeline_runner as PR
from storage import local_db as L
from storage.oci_client import OCIConfig, OCIStorageClient
from tests.test_local_db import _analisis, _lote
from tests.test_oci_client import FakeOCIBackend


def _tel_analisis(degradados: int = 0, total: int = 2) -> TelemetriaAnalisis:
    return TelemetriaAnalisis(
        proveedor="openai", modelo="gpt-x", prompt_version="1.0.0+abc", latencia_ms=900,
        tokens_entrada=400, tokens_salida=100, tokens_cacheados=50,
        total_procesados=total, degradados=degradados,
    )


def _tel_generacion(latencia: int = 300) -> TelemetriaGeneracion:
    return TelemetriaGeneracion(
        proveedor="openai", modelo="gpt-y", prompt_version="2.0.0+def", latencia_ms=latencia,
        tokens_entrada=200, tokens_salida=60, tokens_cacheados=20,
    )


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    db = L.LocalDatabaseManager(tmp_path / "data" / "c.db")
    oci = OCIStorageClient.__new__(OCIStorageClient)
    oci.config = OCIConfig(bucket_name="test-bucket")
    oci._client, oci._namespace = FakeOCIBackend(), "ns"
    oci._log = structlog.get_logger("t")

    def instalar(ids, no_aptos=(), tel_analisis=None, generar=None):
        async def fake_analisis(interacciones, run_id, request_id):
            return _analisis(run_id, request_id, ids, tuple(no_aptos)), tel_analisis or _tel_analisis(total=len(ids))

        async def fake_generar(analisis, interacciones, enfoque, run_id, request_id, temperatura_cero=False):
            aptos = [m.source_id for m in analisis.mensajes_analizados if m.apto_para_publicacion]
            return (
                PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 30, hashtags=["#a"], source_ids=aptos[:1]),
                ResumenSemanal(seccion="Logros", titular="Titular ok", resumen="r" * 30, source_ids=aptos[:1]),
                _tel_generacion(),
            )

        monkeypatch.setattr(PR, "analizar_lote_cognitivo_con_telemetria", fake_analisis)
        monkeypatch.setattr(PR, "generar_activos_marketing", generar or fake_generar)

    return PR.CommunityLabPipeline(oci_client=oci, db_manager=db), db, instalar


def _filas(db, run_id):
    con = sqlite3.connect(db.db_path)
    try:
        return con.execute(
            "SELECT proveedor, modelo, latencia_ms, prompt_tokens, completion_tokens, tokens_totales, tokens_cacheados "
            "FROM telemetria_llamadas_llm WHERE run_id = ? ORDER BY id", (run_id,),
        ).fetchall()
    finally:
        con.close()


def test_completado_persiste_una_fila_por_etapa(entorno):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"])
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-1", ["m1", "m2"]), "run-1"))
    assert r.estado_flujo == "completado"
    assert _filas(db, "run-1") == [
        ("openai", "gpt-x", 900, 400, 100, 500, 50),   # análisis
        ("openai", "gpt-y", 300, 200, 60, 260, 20),    # generación
    ]


def test_reintentos_de_generacion_se_acumulan_en_una_fila(entorno):
    pipeline, db, instalar = entorno
    llamadas = {"n": 0}

    async def generar_con_violacion(analisis, interacciones, enfoque, run_id, request_id, temperatura_cero=False):
        llamadas["n"] += 1
        aptos = [m.source_id for m in analisis.mensajes_analizados if m.apto_para_publicacion]
        ids = ["inexistente"] if llamadas["n"] == 1 else aptos[:1]  # 1.er intento viola trazabilidad
        return (
            PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 30, hashtags=["#a"], source_ids=ids),
            ResumenSemanal(seccion="Logros", titular="Titular ok", resumen="r" * 30, source_ids=aptos[:1]),
            _tel_generacion(latencia=300),
        )

    instalar(["m1", "m2"], generar=generar_con_violacion)
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-2", ["m1", "m2"]), "run-2"))
    assert r.estado_flujo == "completado" and llamadas["n"] == 2
    filas = _filas(db, "run-2")
    assert len(filas) == 2
    assert filas[1][2:] == (600, 400, 120, 520, 40)  # 2 intentos sumados


def test_sin_contenido_publicable_registra_solo_el_analisis(entorno):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"], no_aptos=("m1", "m2"))
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-3", ["m1", "m2"]), "run-3"))
    assert r.estado_flujo == "sin_contenido_publicable"
    assert _filas(db, "run-3") == [("openai", "gpt-x", 900, 400, 100, 500, 50)]


def test_caida_total_del_llm_tambien_deja_telemetria(entorno):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"], no_aptos=("m1", "m2"), tel_analisis=_tel_analisis(degradados=2, total=2))
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-4", ["m1", "m2"]), "run-4"))
    assert r.estado_flujo == "fallido"
    assert len(_filas(db, "run-4")) == 1


def test_reejecucion_reemplaza_en_vez_de_duplicar(entorno):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"])
    for _ in range(2):
        asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-5", ["m1", "m2"]), "run-5"))
    assert len(_filas(db, "run-5")) == 2


def test_un_fallo_al_guardar_telemetria_no_cambia_el_resultado(entorno, monkeypatch):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"])

    def roto(*_a, **_k):
        raise sqlite3.OperationalError("disco lleno")

    monkeypatch.setattr(db, "registrar_telemetria", roto)
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-6", ["m1", "m2"]), "run-6"))
    assert r.estado_flujo == "completado" and r.paquete_salida is not None
    assert _filas(db, "run-6") == []


def test_manager_sin_el_metodo_no_rompe_el_flujo(entorno):
    pipeline, db, instalar = entorno
    instalar(["m1", "m2"])

    class SinTelemetria:
        def __init__(self, base):
            self._base = base

        def __getattr__(self, nombre):
            if nombre == "registrar_telemetria":
                raise AttributeError(nombre)
            return getattr(self._base, nombre)

    pipeline.db_manager = SinTelemetria(db)
    r = asyncio.run(pipeline.ejecutar_desde_lote(_lote("req-7", ["m1", "m2"]), "run-7"))
    assert r.estado_flujo == "completado"
