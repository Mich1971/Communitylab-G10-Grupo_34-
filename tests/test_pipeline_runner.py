import asyncio
import json
from pathlib import Path

from esquemas import AnalisisCognitivo, IngestionLote
from orchestration.pipeline_runner import crear_grafo_pipeline, ejecutar_pipeline
from ui.mock_pipeline import analizar_mock, generar_copys_mock


def _cargar_lote_prueba() -> dict:
    ruta = Path("data/raw/lote_prueba_01.json")
    return json.loads(ruta.read_text(encoding="utf-8"))


def test_lote_prueba_genera_paquete_con_dos_formatos_y_fuentes():
    resultado = asyncio.run(
        ejecutar_pipeline(_cargar_lote_prueba(), run_id="run-test-001")
    )

    assert resultado.estado_flujo == "completado"
    assert resultado.run_id == "run-test-001"
    assert resultado.request_id == resultado.paquete_salida.request_id
    assert resultado.paquete_salida is not None
    assert resultado.mensajes_recibidos == 4
    assert resultado.mensajes_validos == 3
    assert resultado.mensajes_rechazados == 1
    assert len(resultado.registros_rechazados) == 1
    assert resultado.registros_rechazados[0].id == "msg_004"
    assert resultado.auditoria_sqlite_ok is False
    assert (
        resultado.paquete_salida.almacenamiento_oci.status_almacenamiento
        == "no_iniciado"
    )
    assert resultado.paquete_salida.activos.post_linkedin.source_ids
    assert resultado.paquete_salida.activos.resumen_semanal.source_ids == [
        "msg_001",
        "msg_002",
        "msg_003",
    ]


def test_lote_sin_mensajes_validos_finaliza_sin_invocar_servicios():
    datos = _cargar_lote_prueba()
    for interaccion in datos["interacciones"]:
        interaccion["texto"] = "   "
    lote = IngestionLote.model_validate(datos)
    llamadas = {"analizador": 0, "copys": 0, "persistidor": 0}

    def analizador(*args):
        llamadas["analizador"] += 1
        raise AssertionError("El analyzer no debe ejecutarse para un lote vacío")

    def generador(analisis):
        llamadas["copys"] += 1
        raise AssertionError("El generador no debe ejecutarse para un lote vacío")

    def persistidor(paquete):
        llamadas["persistidor"] += 1
        raise AssertionError("La persistencia no debe ejecutarse para un lote vacío")

    resultado = asyncio.run(
        ejecutar_pipeline(
            lote,
            analizador=analizador,
            generador_copys=generador,
            persistidor=persistidor,
        )
    )

    assert resultado.paquete_salida is None
    assert resultado.estado_flujo == "sin_registros_validos"
    assert resultado.motivo == "lote_sin_mensajes_validos"
    assert resultado.mensajes_recibidos == 4
    assert resultado.mensajes_validos == 0
    assert resultado.mensajes_rechazados == 4
    assert resultado.auditoria_sqlite_ok is False
    assert llamadas == {"analizador": 0, "copys": 0, "persistidor": 0}


def test_lote_sin_contenido_publicable_no_genera_paquete_ni_persiste():
    datos = _cargar_lote_prueba()
    for interaccion in datos["interacciones"]:
        interaccion["tipo"] = "Duda"
        if interaccion["texto"].strip():
            interaccion["texto"] = "¿Cómo puedo resolver esta consulta técnica?"

    llamadas = {"copys": 0, "persistidor": 0}

    def generador(analisis):
        llamadas["copys"] += 1
        raise AssertionError("No debe generar copys sin contenido publicable")

    def persistidor(paquete):
        llamadas["persistidor"] += 1
        raise AssertionError("No debe persistir sin paquete")

    resultado = asyncio.run(
        ejecutar_pipeline(
            datos,
            generador_copys=generador,
            persistidor=persistidor,
            run_id="run-test-no-publicable",
        )
    )

    assert resultado.estado_flujo == "sin_contenido_publicable"
    assert resultado.paquete_salida is None
    assert resultado.motivo == "sin_contenido_publicable"
    assert llamadas == {"copys": 0, "persistidor": 0}


def test_analisis_degradado_finaliza_sin_generar_copys_ni_persistir():
    def analizador(validos, run_id, request_id) -> AnalisisCognitivo:
        analisis = analizar_mock(validos, run_id, request_id)
        mensajes = [
            mensaje.model_copy(
                update={
                    "apto_para_publicacion": True,
                    "puntuacion_relevancia": mensaje.puntuacion_relevancia.model_copy(
                        update={
                            "evidencia_explicita": 0,
                            "utilidad_comunitaria": 0,
                            "claridad_contexto": 0,
                            "total": 0,
                        }
                    ),
                }
            )
            for mensaje in analisis.mensajes_analizados
        ]
        return analisis.model_copy(update={"mensajes_analizados": mensajes})

    def no_debe_ejecutarse(*args):
        raise AssertionError("No deben ejecutarse copys ni persistencia")

    resultado = asyncio.run(
        ejecutar_pipeline(
            _cargar_lote_prueba(),
            analizador=analizador,
            generador_copys=no_debe_ejecutarse,
            persistidor=no_debe_ejecutarse,
        )
    )

    assert resultado.estado_flujo == "sin_contenido_publicable"
    assert resultado.motivo == "analisis_degradado"
    assert resultado.paquete_salida is None


def test_error_de_copy_devuelve_fallido_sin_paquete_ni_persistencia():
    def generador(analisis):
        raise RuntimeError("fallo simulado en copys")

    def persistidor(paquete):
        raise AssertionError("No debe persistir si falla la generación de copys")

    resultado = asyncio.run(
        ejecutar_pipeline(
            _cargar_lote_prueba(),
            generador_copys=generador,
            persistidor=persistidor,
            run_id="run-test-error",
        )
    )

    assert resultado.estado_flujo == "fallido"
    assert resultado.run_id == "run-test-error"
    assert resultado.paquete_salida is None
    assert resultado.errores == ["fallo simulado en copys"]
    assert resultado.mensajes_validos == 3
    assert resultado.mensajes_rechazados == 1


def test_copy_none_se_reporta_como_fallido_en_vez_de_fabricar_activos():
    resultado = asyncio.run(
        ejecutar_pipeline(
            _cargar_lote_prueba(),
            generador_copys=lambda analisis: None,
        )
    )

    assert resultado.estado_flujo == "fallido"
    assert resultado.paquete_salida is None
    assert resultado.errores == ["El generador de copys no produjo contenido"]


def test_validacion_no_marca_pipeline_completado_antes_del_final():
    async def capturar_actualizaciones():
        grafo = crear_grafo_pipeline()
        return [
            actualizacion
            async for actualizacion in grafo.astream(
                {
                    "payload": IngestionLote.model_validate(_cargar_lote_prueba()),
                    "run_id": "run-test-intermedio",
                },
                stream_mode="updates",
            )
        ]

    actualizaciones = asyncio.run(capturar_actualizaciones())
    validacion = next(
        actualizacion["validar_datos"]
        for actualizacion in actualizaciones
        if "validar_datos" in actualizacion
    )

    assert "estado_flujo" not in validacion


def test_source_ids_vacios_o_desconocidos_fallan_sin_persistir():
    for source_ids in ([], ["source-ajeno"]):
        llamadas_persistidor = 0

        def generador(analisis, ids=source_ids):
            activos = generar_copys_mock(analisis)
            post = activos.post_linkedin.model_copy(update={"source_ids": ids})
            return activos.model_copy(update={"post_linkedin": post})

        def persistidor(paquete):
            nonlocal llamadas_persistidor
            llamadas_persistidor += 1
            raise AssertionError("No debe persistirse un paquete con fuentes inválidas")

        resultado = asyncio.run(
            ejecutar_pipeline(
                _cargar_lote_prueba(),
                generador_copys=generador,
                persistidor=persistidor,
            )
        )

        assert resultado.estado_flujo == "fallido"
        assert resultado.paquete_salida is None
        assert "source_ids" in resultado.errores[0]
        assert llamadas_persistidor == 0


def test_assertion_error_del_componente_no_se_convierte_en_resultado_fallido():
    def analizador(*args):
        raise AssertionError("error de programación en analyzer")

    try:
        asyncio.run(ejecutar_pipeline(_cargar_lote_prueba(), analizador=analizador))
    except AssertionError as error:
        assert str(error) == "error de programación en analyzer"
    else:
        raise AssertionError("AssertionError debe propagarse al llamador")


def test_topologia_tiene_una_sola_bifurcacion_por_lote_vacio():
    grafo = crear_grafo_pipeline().get_graph()
    aristas_por_origen: dict[str, set[str]] = {}
    for arista in grafo.edges:
        aristas_por_origen.setdefault(arista.source, set()).add(arista.target)

    bifurcaciones = {
        origen: destinos
        for origen, destinos in aristas_por_origen.items()
        if len(destinos) > 1
    }

    assert bifurcaciones == {
        "validar_datos": {"analisis_cognitivo", "finalizar_lote_vacio"},
        "analisis_cognitivo": {"generar_copys", "finalizar_sin_contenido"},
    }
    assert "enrutar_editorial" not in grafo.nodes


def test_grafo_por_defecto_se_reutiliza():
    assert crear_grafo_pipeline() is crear_grafo_pipeline()


def test_grafos_con_dependencias_inyectadas_no_se_reutilizan():
    def analizador_personalizado(validos, run_id, request_id):
        return analizar_mock(validos, run_id, request_id)

    primero = crear_grafo_pipeline(analizador=analizador_personalizado)
    segundo = crear_grafo_pipeline(analizador=analizador_personalizado)

    assert primero is not segundo
