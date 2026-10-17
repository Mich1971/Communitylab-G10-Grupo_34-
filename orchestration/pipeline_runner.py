"""Orquestación LangGraph de Fase 1, con analyzer/copy/persistencia inyectables."""

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from threading import Lock
from typing import Any

from langgraph.graph import END, START, StateGraph

from data_pipeline.validador import adaptar_payload_oficial, validar_lote_crudo
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    EvidenciaAlmacenamientoOCI,
    IngestionLote,
    InteraccionValidada,
    MetadatosEjecucion,
    PaqueteSalida,
)
from orchestration.state import (
    PipelineState,
    RegistroRechazado,
    ResultadoPipeline,
)
from ui.mock_pipeline import analizar_mock, generar_copys_mock, generar_run_id

Analizador = Callable[
    [list[InteraccionValidada], str, str],
    AnalisisCognitivo | Awaitable[AnalisisCognitivo],
]
GeneradorCopys = Callable[
    [AnalisisCognitivo], ActivosGenerados | None | Awaitable[ActivosGenerados | None]
]
Persistidor = Callable[[PaqueteSalida], bool | Awaitable[bool]]

_GRAFO_PIPELINE_DEFAULT: Any | None = None
_GRAFO_PIPELINE_DEFAULT_LOCK = Lock()


def _normalizar_payload(payload: IngestionLote | dict[str, Any]) -> IngestionLote:
    if isinstance(payload, IngestionLote):
        return payload
    if not isinstance(payload, dict):
        raise TypeError("payload debe ser IngestionLote o un diccionario")

    interacciones = payload.get("interacciones")
    es_sobre_oficial = (
        "request_id" in payload
        and isinstance(interacciones, list)
        and all(
            isinstance(item, dict) and "id" in item and "metadata_origen" in item
            for item in interacciones
        )
    )
    datos = payload if es_sobre_oficial else adaptar_payload_oficial(payload)
    return IngestionLote.model_validate(datos)


def _enrutar_lote(state: PipelineState) -> str:
    return "continuar" if state["validos"] else "lote_vacio"


async def _resolver(resultado: Any) -> Any:
    return await resultado if isawaitable(resultado) else resultado


def _validar_source_ids(
    activos: ActivosGenerados, analisis: AnalisisCognitivo
) -> ActivosGenerados:
    disponibles = {mensaje.source_id for mensaje in analisis.mensajes_analizados}

    for nombre, activo in (
        ("post_linkedin", activos.post_linkedin),
        ("resumen_semanal", activos.resumen_semanal),
    ):
        source_ids = activo.source_ids
        if not source_ids:
            raise ValueError(f"{nombre}.source_ids no puede estar vacío")
        ids_desconocidos = sorted(set(source_ids) - disponibles)
        if ids_desconocidos:
            raise ValueError(
                f"{nombre}.source_ids contiene fuentes no analizadas: "
                f"{ids_desconocidos}"
            )
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"{nombre}.source_ids contiene fuentes duplicadas")

    return activos


def _crear_paquete(state: PipelineState) -> PaqueteSalida:
    payload = state["payload"]
    run_id = state["run_id"]
    return PaqueteSalida(
        run_id=run_id,
        request_id=payload.request_id,
        metadatos_ejecucion=MetadatosEjecucion(
            modelo="mock",
            prompt_version="orquestador-fase-1",
            latencia_ms=0,
            tokens_totales=0,
        ),
        analisis_resumido=state["analisis"].resumen_comunidad,
        activos=state["activos"],
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            ruta_objeto=(
                f"activos/{payload.periodo_referencia}/{run_id}/revision-001.json"
            ),
            status_almacenamiento="no_iniciado",
            comprobacion_lectura=False,
        ),
    )


def _persistencia_stub(paquete: PaqueteSalida) -> bool:
    """Marca la auditoría local pendiente; nunca contacta SQLite ni OCI."""
    del paquete
    return False


def _normalizar_rechazos(rechazados: list[dict[str, Any]]) -> list[RegistroRechazado]:
    return [RegistroRechazado.model_validate(registro) for registro in rechazados]


def _enrutar_analisis(state: PipelineState) -> str:
    mensajes = state["analisis"].mensajes_analizados
    analisis_degradado = all(
        mensaje.puntuacion_relevancia.total == 0 for mensaje in mensajes
    )
    if analisis_degradado or not any(
        mensaje.apto_para_publicacion for mensaje in mensajes
    ):
        return "sin_contenido_publicable"
    return "continuar"


def _motivo_sin_contenido(analisis: AnalisisCognitivo) -> str:
    if all(
        mensaje.puntuacion_relevancia.total == 0
        for mensaje in analisis.mensajes_analizados
    ):
        return "analisis_degradado"
    return "sin_contenido_publicable"


def crear_grafo_pipeline(
    *,
    analizador: Analizador = analizar_mock,
    generador_copys: GeneradorCopys = generar_copys_mock,
    persistidor: Persistidor = _persistencia_stub,
):
    """Compila la topología M01 con implementaciones reemplazables por inyección."""
    global _GRAFO_PIPELINE_DEFAULT

    usa_implementaciones_default = (
        analizador is analizar_mock
        and generador_copys is generar_copys_mock
        and persistidor is _persistencia_stub
    )
    if usa_implementaciones_default:
        with _GRAFO_PIPELINE_DEFAULT_LOCK:
            if _GRAFO_PIPELINE_DEFAULT is not None:
                return _GRAFO_PIPELINE_DEFAULT

    def validar_datos(state: PipelineState) -> dict[str, Any]:
        validos, rechazos_crudos = validar_lote_crudo(state["payload"])
        rechazados = _normalizar_rechazos(rechazos_crudos)
        actualizacion = {
            "validos": validos,
            "rechazados": rechazados,
            "mensajes_recibidos": len(state["payload"].interacciones),
            "mensajes_validos": len(validos),
            "mensajes_rechazados": len(rechazados),
            "errores": [],
            "auditoria_sqlite_ok": False,
        }
        if not validos:
            actualizacion["estado_flujo"] = "sin_registros_validos"
            actualizacion["motivo"] = "lote_sin_mensajes_validos"
        return actualizacion

    async def analizar(state: PipelineState) -> dict[str, Any]:
        analisis = await _resolver(
            analizador(
                state["validos"],
                state["run_id"],
                state["payload"].request_id,
            )
        )
        analisis = AnalisisCognitivo.model_validate(analisis)
        analisis.resumen_comunidad.registros_rechazados = state["mensajes_rechazados"]
        analisis.resumen_comunidad.total_interacciones_procesadas = state[
            "mensajes_recibidos"
        ]
        return {"analisis": analisis}

    async def generar_copys(state: PipelineState) -> dict[str, Any]:
        activos = await _resolver(generador_copys(state["analisis"]))
        if activos is None:
            raise ValueError("El generador de copys no produjo contenido")
        activos = ActivosGenerados.model_validate(activos)
        return {"activos": _validar_source_ids(activos, state["analisis"])}

    async def persistencia_dual(state: PipelineState) -> dict[str, Any]:
        paquete = _crear_paquete(state)
        auditoria_ok = await _resolver(persistidor(paquete))
        return {
            "paquete_salida": paquete,
            "estado_flujo": "completado",
            "motivo": None,
            "auditoria_sqlite_ok": bool(auditoria_ok),
        }

    def finalizar_lote_vacio(state: PipelineState) -> dict[str, Any]:
        return {
            "paquete_salida": None,
            "estado_flujo": "sin_registros_validos",
            "motivo": "lote_sin_mensajes_validos",
            "auditoria_sqlite_ok": False,
        }

    def finalizar_sin_contenido(state: PipelineState) -> dict[str, Any]:
        return {
            "paquete_salida": None,
            "estado_flujo": "sin_contenido_publicable",
            "motivo": _motivo_sin_contenido(state["analisis"]),
            "auditoria_sqlite_ok": False,
        }

    grafo = StateGraph(PipelineState)
    grafo.add_node("validar_datos", validar_datos)
    grafo.add_node("analisis_cognitivo", analizar)
    grafo.add_node("generar_copys", generar_copys)
    grafo.add_node("persistencia_dual", persistencia_dual)
    grafo.add_node("finalizar_lote_vacio", finalizar_lote_vacio)
    grafo.add_node("finalizar_sin_contenido", finalizar_sin_contenido)
    grafo.add_edge(START, "validar_datos")
    grafo.add_conditional_edges(
        "validar_datos",
        _enrutar_lote,
        {
            "continuar": "analisis_cognitivo",
            "lote_vacio": "finalizar_lote_vacio",
        },
    )
    grafo.add_conditional_edges(
        "analisis_cognitivo",
        _enrutar_analisis,
        {
            "continuar": "generar_copys",
            "sin_contenido_publicable": "finalizar_sin_contenido",
        },
    )
    grafo.add_edge("generar_copys", "persistencia_dual")
    grafo.add_edge("persistencia_dual", END)
    grafo.add_edge("finalizar_lote_vacio", END)
    grafo.add_edge("finalizar_sin_contenido", END)
    grafo_compilado = grafo.compile()
    if not usa_implementaciones_default:
        return grafo_compilado

    with _GRAFO_PIPELINE_DEFAULT_LOCK:
        if _GRAFO_PIPELINE_DEFAULT is None:
            _GRAFO_PIPELINE_DEFAULT = grafo_compilado
        return _GRAFO_PIPELINE_DEFAULT


async def ejecutar_pipeline(
    payload: IngestionLote | dict[str, Any],
    *,
    analizador: Analizador = analizar_mock,
    generador_copys: GeneradorCopys = generar_copys_mock,
    persistidor: Persistidor = _persistencia_stub,
    run_id: str | None = None,
) -> ResultadoPipeline:
    lote = _normalizar_payload(payload)
    id_ejecucion = run_id or generar_run_id(lote.periodo_referencia)
    try:
        estado = await crear_grafo_pipeline(
            analizador=analizador,
            generador_copys=generador_copys,
            persistidor=persistidor,
        ).ainvoke(
            {
                "payload": lote,
                "run_id": id_ejecucion,
            }
        )
    except AssertionError:
        raise
    except Exception as error:  # noqa: BLE001 - expose pipeline failures in the result envelope
        try:
            validos, rechazados_crudos = validar_lote_crudo(lote)
            rechazados = _normalizar_rechazos(rechazados_crudos)
        except Exception:  # noqa: BLE001 - preserve the original pipeline failure
            validos, rechazados = [], []
        return ResultadoPipeline(
            estado_flujo="fallido",
            run_id=id_ejecucion,
            request_id=lote.request_id,
            registros_rechazados=rechazados,
            errores=[str(error)],
            paquete_salida=None,
            motivo="error_pipeline",
            mensajes_recibidos=len(lote.interacciones),
            mensajes_validos=len(validos),
            mensajes_rechazados=len(rechazados),
            auditoria_sqlite_ok=False,
        )
    return ResultadoPipeline(
        estado_flujo=estado["estado_flujo"],
        run_id=estado["run_id"],
        request_id=lote.request_id,
        registros_rechazados=estado.get("rechazados", []),
        errores=estado.get("errores", []),
        paquete_salida=estado.get("paquete_salida"),
        motivo=estado.get("motivo"),
        mensajes_recibidos=estado["mensajes_recibidos"],
        mensajes_validos=estado["mensajes_validos"],
        mensajes_rechazados=estado["mensajes_rechazados"],
        auditoria_sqlite_ok=estado["auditoria_sqlite_ok"],
    )
