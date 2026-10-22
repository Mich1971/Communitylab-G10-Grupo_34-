"""Pruebas de `orchestration/resultado.py` y `orchestration/state.py` (spec v3.1, Fase 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from esquemas import (
    ActivosGenerados,
    EvidenciaAlmacenamientoOCI,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    ResumenComunidad,
    ResumenSemanal,
)
from orchestration.resultado import RegistroRechazado, ResultadoPipeline
from orchestration.state import PipelineState


def _paquete() -> PaqueteSalida:
    return PaqueteSalida(
        run_id="run-1",
        request_id="req-1",
        metadatos_ejecucion=MetadatosEjecucion(modelo="m", prompt_version="1", latencia_ms=1, tokens_totales=1),
        analisis_resumido=ResumenComunidad(
            total_interacciones_procesadas=1, registros_validos=1, sentimiento_predominante="Positivo",
            distribucion_sentimiento={"Positivo": 1}, temas_principales=["python"],
        ),
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 25, source_ids=["m1"]),
            resumen_semanal=ResumenSemanal(seccion="s", titular="Titular ok", resumen="r" * 25, source_ids=["m1"]),
        ),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(ruta_objeto="activos/p/run-1/revision-001.json"),
    )


@pytest.mark.parametrize("estado", ["sin_registros_validos", "sin_contenido_publicable", "fallido"])
def test_resultado_sin_paquete_es_valido_para_estados_sin_contenido(estado):
    r = ResultadoPipeline(estado_flujo=estado, run_id="run-1", request_id="req-1")
    assert r.paquete_salida is None and r.errores == [] and r.casos_no_publicables == []


def test_completado_exige_paquete():
    with pytest.raises(ValidationError):
        ResultadoPipeline(estado_flujo="completado", run_id="run-1", request_id="req-1")
    r = ResultadoPipeline(estado_flujo="completado", run_id="run-1", request_id="req-1", paquete_salida=_paquete())
    assert r.paquete_salida is not None


@pytest.mark.parametrize("estado", ["sin_registros_validos", "sin_contenido_publicable", "fallido"])
def test_paquete_adjunto_a_un_estado_sin_contenido_es_invalido(estado):
    with pytest.raises(ValidationError):
        ResultadoPipeline(estado_flujo=estado, run_id="run-1", request_id="req-1", paquete_salida=_paquete())


def test_estado_flujo_final_solo_admite_los_cuatro_terminales():
    with pytest.raises(ValidationError):
        ResultadoPipeline(estado_flujo="analizado", run_id="run-1", request_id="req-1")


def test_resultado_no_agrega_campos_fuera_de_la_spec():
    assert set(ResultadoPipeline.model_fields) == {
        "estado_flujo", "run_id", "request_id", "registros_rechazados",
        "casos_no_publicables", "errores", "paquete_salida",
    }
    assert set(RegistroRechazado.model_fields) == {"mensaje_id", "autor", "canal", "error_detalle"}


def test_pipeline_state_declara_los_campos_de_la_spec():
    esperados = {
        "run_id", "request_id", "origen_comunidad", "periodo_referencia", "ruta_archivo_crudo",
        "interacciones_crudas", "interacciones_validas", "registros_rechazados",
        "analisis_cognitivo", "telemetria_analisis", "enfoque_editorial", "conteo_enfoque_editorial",
        "activos_generados", "casos_no_publicables", "metadatos_ejecucion",
        "paquete_salida", "almacenamiento_oci", "errores", "estado_flujo",
    }
    assert set(PipelineState.__annotations__) == esperados
