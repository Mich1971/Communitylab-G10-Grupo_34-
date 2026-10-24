"""Orquestador de pipeline y máquina de estados (LangGraph) — spec v3.1.

Coordina: validación -> análisis cognitivo -> generación de copys ->
persistencia dual (SQLite + OCI), con dos bifurcaciones condicionales de
control de flujo (nunca de formato de salida, Regla M01) y dos nodos
auxiliares para los caminos sin paquete.

Principio de diseño: el Contrato 3 (`PaqueteSalida`) solo se construye cuando
existe material real para todos sus campos obligatorios. Por eso el orquestador
devuelve un `ResultadoPipeline`, que contiene el paquete únicamente cuando el
flujo terminó en `completado`.

Este módulo no responde HTTP ni vigila `data/raw/`: lo invoca el worker
(`orchestration/worker.py`) mediante `ejecutar_desde_archivo`.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langgraph.graph import END, StateGraph

from ai_modules.cognitive_analyzer import (
    analizar_lote_cognitivo_con_telemetria,
    tiene_analisis_utilizable,
)
from ai_modules.generador import TelemetriaGeneracion, generar_activos_marketing
from data_pipeline.validador import adaptar_payload_oficial, validar_lote_crudo
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EstadoRevision,
    EvidenciaAlmacenamientoOCI,
    IngestionLote,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    ResumenSemanal,
)
from orchestration.resultado import RegistroRechazado, ResultadoPipeline
from orchestration.state import PipelineState
from storage.local_db import LocalDatabaseManager
from storage.oci_client import OCIStorageClient

logger = structlog.get_logger("orchestration.pipeline_runner")

NUMERO_REVISION_INICIAL = 1
MAX_REINTENTOS_INVARIANTES = 2  # reintentos del prompt con temperatura cero (sección 4, Nodo 3, paso 6)
PRIORIDAD_EMPATE_ENFOQUE = ("Logro", "Dificultad", "Duda")  # a ratificar con el equipo editorial
BUCKET_POR_DEFECTO = "communitylab-activos-marketing"
DESCONOCIDO = "desconocido"

_ESTADOS_TERMINALES = frozenset(
    {"completado", "sin_registros_validos", "sin_contenido_publicable", "fallido"}
)
_NOMBRE_RUN = re.compile(r"^run-\d{8}T\d{6}Z-[0-9a-f]{12}$")


class InvarianteViolada(RuntimeError):
    """Una invariante de trazabilidad factual (sección 6.3) no se cumple."""


# ---------------------------------------------------------------------------
# Funciones puras (sin I/O): enfoque editorial, invariantes y metadatos
# ---------------------------------------------------------------------------


def calcular_enfoque_editorial(analisis: AnalisisCognitivo) -> tuple[str, dict[str, int]]:
    """Enfoque editorial (M01): categoría mayoritaria entre los mensajes aptos.

    En empate gana `Logro` > `Dificultad` > `Duda`. Solo orienta el tono; nunca
    bifurca el formato de salida. Devuelve `(enfoque, conteo)`.
    """
    conteo: dict[str, int] = {"Logro": 0, "Duda": 0, "Dificultad": 0}
    for m in analisis.mensajes_analizados:
        if m.apto_para_publicacion:
            conteo[m.categoria_enrutamiento] += 1
    maximo = max(conteo.values())
    enfoque = next(c for c in PRIORIDAD_EMPATE_ENFOQUE if conteo[c] == maximo)
    return enfoque, conteo


def invariante_casos(analisis: AnalisisCognitivo, casos: list[CasoNoPublicable]) -> bool:
    """`casos_no_publicables` debe ser exactamente el conjunto de no aptos, sin repetidos."""
    no_aptos = {m.source_id for m in analisis.mensajes_analizados if not m.apto_para_publicacion}
    ids = [c.source_id for c in casos]
    return len(ids) == len(set(ids)) and set(ids) == no_aptos


def violaciones_trazabilidad(
    analisis: AnalisisCognitivo, post: PostLinkedIn, resumen: ResumenSemanal
) -> list[str]:
    """Invariantes 1 y 2 de la sección 6.3 (las del LLM). Lista vacía si se cumplen.

    * `PostLinkedIn` solo puede citar mensajes aptos (A).
    * `ResumenSemanal` puede citar cualquier mensaje procesado (M).
    * Ningún identificador puede ser vacío ni inexistente.
    """
    procesados = {m.source_id for m in analisis.mensajes_analizados}
    aptos = {m.source_id for m in analisis.mensajes_analizados if m.apto_para_publicacion}
    problemas: list[str] = []

    for nombre, activo, permitidos in (
        ("post_linkedin", post, aptos),
        ("resumen_semanal", resumen, procesados),
    ):
        ids = list(activo.source_ids)
        if not ids:
            problemas.append(f"{nombre}.source_ids está vacío")
        vacios = [s for s in ids if not s or not str(s).strip()]
        if vacios:
            problemas.append(f"{nombre}.source_ids contiene identificadores vacíos")
        fuera = sorted({s for s in ids if s and s not in permitidos})
        if fuera:
            regla = "fuera del conjunto de mensajes aptos" if nombre == "post_linkedin" else "inexistentes"
            problemas.append(f"{nombre}.source_ids {regla}: {fuera}")
    return problemas


def combinar_metadatos(
    telemetria_analisis: Any, telemetria_generacion: TelemetriaGeneracion | None
) -> MetadatosEjecucion:
    """Compone el único `MetadatosEjecucion` del paquete (sección 8, punto 11).

    Propuesta de la spec: sumar `latencia_ms` y `tokens_totales` y combinar
    `modelo` y `prompt_version` de ambas etapas (p. ej. "gpt-4o-mini + gpt-4o-mini").
    """
    base = telemetria_analisis.a_metadatos_ejecucion()
    if telemetria_generacion is None:
        return base
    gen = telemetria_generacion.a_metadatos_ejecucion()
    return MetadatosEjecucion(
        modelo=f"{base.modelo} + {gen.modelo}",
        prompt_version=f"{base.prompt_version} + {gen.prompt_version}",
        latencia_ms=base.latencia_ms + gen.latencia_ms,
        tokens_totales=base.tokens_totales + gen.tokens_totales,
    )


def _sumar_telemetrias(lista: list[TelemetriaGeneracion]) -> TelemetriaGeneracion:
    """Acumula los intentos (incluidos los reintentos por invariantes) en una sola telemetría."""
    ultima = lista[-1]
    return TelemetriaGeneracion(
        proveedor=ultima.proveedor,
        modelo=ultima.modelo,
        prompt_version=ultima.prompt_version,
        latencia_ms=sum(t.latencia_ms for t in lista),
        tokens_entrada=sum(t.tokens_entrada for t in lista),
        tokens_salida=sum(t.tokens_salida for t in lista),
        tokens_cacheados=sum(t.tokens_cacheados for t in lista),
    )


def _es_sobre_oficial(payload: dict[str, Any]) -> bool:
    interacciones = payload.get("interacciones")
    return (
        "request_id" in payload
        and isinstance(interacciones, list)
        and all(isinstance(i, dict) and "id" in i and "metadata_origen" in i for i in interacciones)
    )


def _a_registro_rechazado(fila: dict[str, Any]) -> RegistroRechazado:
    return RegistroRechazado(
        mensaje_id=fila.get("mensaje_id") or fila.get("id"),
        autor=fila.get("autor"),
        canal=fila.get("canal"),
        error_detalle=str(fila.get("error_detalle") or fila.get("error") or "Error desconocido de validación"),
    )


def _generar_run_id() -> str:
    marca = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{marca}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------


class CommunityLabPipeline:
    """Coordinador del grafo de ejecución LangGraph para CommunityLab."""

    def __init__(
        self,
        oci_client: OCIStorageClient | None = None,
        db_manager: LocalDatabaseManager | None = None,
    ) -> None:
        self.oci_client = oci_client or OCIStorageClient()
        self.db_manager = db_manager or LocalDatabaseManager()
        # `request_id` leído por el Nodo 1 cuando el lote entra por archivo: si un nodo
        # posterior lanza, `inicial["request_id"]` sigue vacío y se recupera de aquí.
        self._request_id_por_run: dict[str, str] = {}
        self.workflow = self._construir_grafo()
        self.app = self.workflow.compile()

    # -- grafo -----------------------------------------------------------------

    def _construir_grafo(self) -> StateGraph:
        """Ensambla los nodos y transiciones del grafo (sección 2)."""
        builder = StateGraph(PipelineState)

        builder.add_node("validar_datos", self._nodo_validar_datos)
        builder.add_node("analisis_cognitivo", self._nodo_analisis_cognitivo)
        builder.add_node("generar_copys", self._nodo_generar_copys)
        builder.add_node("persistencia_dual", self._nodo_persistencia_dual)
        builder.add_node("finalizar_lote_vacio", self._nodo_finalizar_lote_vacio)
        builder.add_node("finalizar_sin_contenido", self._nodo_finalizar_sin_contenido)

        builder.set_entry_point("validar_datos")

        # Bifurcación 1: lote sin registros válidos.
        builder.add_conditional_edges(
            "validar_datos",
            self._decidir_continuacion_datos,
            {"continuar": "analisis_cognitivo", "vacio": "finalizar_lote_vacio"},
        )
        # Bifurcación 2: análisis sin ningún mensaje apto. El enfoque editorial
        # NO es una arista condicional (M01).
        builder.add_conditional_edges(
            "analisis_cognitivo",
            self._decidir_tras_analisis,
            {"continuar": "generar_copys", "sin_contenido": "finalizar_sin_contenido"},
        )

        builder.add_edge("generar_copys", "persistencia_dual")
        builder.add_edge("persistencia_dual", END)
        builder.add_edge("finalizar_lote_vacio", END)
        builder.add_edge("finalizar_sin_contenido", END)

        return builder

    # -- aristas condicionales -------------------------------------------------

    def _decidir_continuacion_datos(self, state: PipelineState) -> str:
        return "vacio" if len(state["interacciones_validas"]) == 0 else "continuar"

    def _decidir_tras_analisis(self, state: PipelineState) -> str:
        analisis = state["analisis_cognitivo"]
        hay_aptos = analisis is not None and any(
            m.apto_para_publicacion for m in analisis.mensajes_analizados
        )
        return "continuar" if hay_aptos else "sin_contenido"

    # -- utilidades de log -------------------------------------------------------

    @staticmethod
    def _log(state: PipelineState):
        return logger.bind(run_id=state["run_id"], request_id=state["request_id"])

    def _transicion(self, state: PipelineState, nuevo_estado: str, **extra: Any) -> None:
        """Un evento de log por cada transición de `estado_flujo` (sección 8, punto 6)."""
        self._log(state).info(
            "transicion_estado_flujo",
            estado_anterior=state["estado_flujo"],
            estado_nuevo=nuevo_estado,
            **extra,
        )

    # -- Nodo 1 ------------------------------------------------------------------

    async def _nodo_validar_datos(self, state: PipelineState) -> dict[str, Any]:
        run_id = state["run_id"]
        errores = list(state["errores"])
        crudas = list(state["interacciones_crudas"])
        request_id = state["request_id"]
        origen = state["origen_comunidad"]
        periodo = state["periodo_referencia"]
        ruta = state["ruta_archivo_crudo"]

        payload: Any = None
        if ruta and not crudas:
            # Lectura y parseo dentro del nodo (spec 4, Nodo 1, paso 1).
            try:
                texto = await asyncio.to_thread(Path(ruta).read_text, encoding="utf-8")
                payload = json.loads(texto)
            except FileNotFoundError:
                errores.append(f"archivo de lote no encontrado: {ruta}")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                errores.append(f"archivo de lote ilegible o con JSON mal formado ({ruta}): {exc}")
        else:
            payload = {
                "request_id": request_id,
                "origen_comunidad": origen,
                "periodo_referencia": periodo,
                "interacciones": crudas,
            }

        lote: IngestionLote | None = None
        if payload is not None:
            try:
                if not isinstance(payload, dict):
                    raise TypeError("el lote debe ser un objeto JSON")
                if not _es_sobre_oficial(payload):
                    # Formato plano del jurado (spec 4, Nodo 1, paso 2).
                    payload = adaptar_payload_oficial(payload)
                lote = IngestionLote.model_validate(payload)
            except Exception as exc:  # noqa: BLE001 — cualquier sobre inválido equivale a lote sin registros válidos
                errores.append(f"lote inválido, ningún registro válido: {exc}")

        if lote is None:
            await asyncio.to_thread(
                self.db_manager.registrar_lote,
                run_id, request_id or DESCONOCIDO, origen or DESCONOCIDO, periodo or DESCONOCIDO,
                0, 0, 0, [],
            )
            self._transicion(state, "validado", validos=0, rechazados=0)
            return {
                "interacciones_validas": [],
                "registros_rechazados": [],
                "errores": errores,
                "estado_flujo": "validado",
            }

        self._request_id_por_run[run_id] = lote.request_id
        validos, rechazados = validar_lote_crudo(lote, run_id=run_id)
        # Los rechazos se persisten de inmediato, antes de cualquier paso que pueda fallar.
        await asyncio.to_thread(
            self.db_manager.registrar_lote,
            run_id, lote.request_id, lote.origen_comunidad, lote.periodo_referencia,
            len(lote.interacciones), len(validos), len(rechazados), rechazados,
        )
        self._transicion(state, "validado", validos=len(validos), rechazados=len(rechazados))
        return {
            "request_id": lote.request_id,
            "origen_comunidad": lote.origen_comunidad,
            "periodo_referencia": lote.periodo_referencia,
            "interacciones_crudas": [i.model_dump(mode="json") for i in lote.interacciones],
            "interacciones_validas": validos,
            "registros_rechazados": rechazados,
            "errores": errores,
            "estado_flujo": "validado",
        }

    # -- Nodo 2 ------------------------------------------------------------------

    async def _nodo_analisis_cognitivo(self, state: PipelineState) -> dict[str, Any]:
        analisis, telemetria = await analizar_lote_cognitivo_con_telemetria(
            state["interacciones_validas"], state["run_id"], state["request_id"]
        )
        self._transicion(state, "analizado", degradados=telemetria.degradados)
        return {
            "analisis_cognitivo": analisis,
            "telemetria_analisis": telemetria,
            "estado_flujo": "analizado",
        }

    # -- Nodo 3 ------------------------------------------------------------------

    async def _nodo_generar_copys(self, state: PipelineState) -> dict[str, Any]:
        analisis = state["analisis_cognitivo"]
        if analisis is None:
            raise InvarianteViolada("nodo_generar_copys requiere un análisis cognitivo")

        # 1. Enfoque editorial (paso interno; orienta tono, no formato — M01).
        enfoque, conteo = calcular_enfoque_editorial(analisis)
        bound_log = self._log(state).bind(
            enfoque_editorial=enfoque, conteo_enfoque_editorial=conteo
        )

        # 4. Casos no publicables: determinista, sin LLM.
        casos = [
            CasoNoPublicable.desde_mensaje_analizado(m)
            for m in analisis.mensajes_analizados
            if not m.apto_para_publicacion
        ]
        if not invariante_casos(analisis, casos):
            # Error de programación (no del LLM): el flujo termina en `fallido`.
            raise InvarianteViolada("casos_no_publicables no coincide con el conjunto de mensajes no aptos")

        # 2-3. Ambos formatos en paralelo; 6. invariantes con reintento a temperatura cero.
        telemetrias: list[TelemetriaGeneracion] = []
        post: PostLinkedIn | None = None
        resumen: ResumenSemanal | None = None
        problemas: list[str] = []
        for intento in range(MAX_REINTENTOS_INVARIANTES + 1):
            post, resumen, telemetria_gen = await generar_activos_marketing(
                analisis,
                state["interacciones_validas"],
                enfoque,
                state["run_id"],
                state["request_id"],
                temperatura_cero=intento > 0,
            )
            telemetrias.append(telemetria_gen)
            problemas = violaciones_trazabilidad(analisis, post, resumen)
            if not problemas:
                break
            bound_log.warning("activo_rechazado_por_invariante", intento=intento + 1, problemas=problemas)
        else:
            raise InvarianteViolada(
                "el generador violó la trazabilidad factual tras reintentar con temperatura cero: "
                + "; ".join(problemas)
            )
        assert post is not None and resumen is not None

        # 5. Se construye la instancia (no `model_copy(update=...)`, que omite validación).
        activos = ActivosGenerados(
            post_linkedin=post, resumen_semanal=resumen, casos_no_publicables=casos
        )
        telemetria_gen_total = _sumar_telemetrias(telemetrias)
        metadatos = combinar_metadatos(state["telemetria_analisis"], telemetria_gen_total)
        bound_log.info("transicion_estado_flujo", estado_anterior=state["estado_flujo"],
                       estado_nuevo="copys_generados", casos_no_publicables=len(casos))
        return {
            "enfoque_editorial": enfoque,
            "conteo_enfoque_editorial": conteo,
            "activos_generados": activos,
            "casos_no_publicables": casos,
            "metadatos_ejecucion": metadatos,
            "telemetria_generacion": telemetria_gen_total,
            "estado_flujo": "copys_generados",
        }

    # -- Telemetría ----------------------------------------------------------------

    async def _registrar_telemetria(self, state: PipelineState) -> None:
        """Persiste una fila por etapa en `telemetria_llamadas_llm` (análisis y, si existe,
        generación). Best-effort: la telemetría nunca debe abortar ni cambiar el estado del flujo.

        El gestor REEMPLAZA las filas del run, por eso siempre se envía la lista completa."""
        registros = [
            t for t in (state.get("telemetria_analisis"), state.get("telemetria_generacion")) if t is not None
        ]
        if not registros:
            return
        try:
            await asyncio.to_thread(
                self.db_manager.registrar_telemetria, state["run_id"], state["request_id"], registros
            )
        except Exception as exc:  # noqa: BLE001 — métrica operativa, no dato de negocio
            self._log(state).warning("telemetria_no_persistida", error=str(exc))

    # -- Nodo 4 ------------------------------------------------------------------

    def _ruta_objeto(self, periodo: str, run_id: str) -> str:
        try:
            return self.oci_client.build_object_path(periodo, run_id, NUMERO_REVISION_INICIAL)
        except Exception:  # noqa: BLE001 — la ruta es informativa; no debe abortar el flujo (M07)
            return f"activos/{periodo}/{run_id}/revision-{NUMERO_REVISION_INICIAL:03d}.json"

    def _bucket(self) -> str:
        bucket = getattr(getattr(self.oci_client, "config", None), "bucket_name", None)
        return bucket if isinstance(bucket, str) and bucket else BUCKET_POR_DEFECTO

    def _conservar_en_disco(self, paquete: PaqueteSalida) -> Path | None:
        """Copia local para `retry_upload_from_disk()` cuando OCI falla (M07).

        El nombre `{run_id}-revision-001.json` es solo informativo: el reintento recibe la
        ruta explícita junto con `run_id`, `request_id` (ambos dentro del JSON),
        `periodo_referencia` y `numero_revision` (ambos en el log de advertencia).
        """
        db_path = getattr(self.db_manager, "db_path", None)
        if not isinstance(db_path, str):
            return None
        destino = Path(db_path).resolve().parent / "pending_oci"
        try:
            destino.mkdir(parents=True, exist_ok=True)
            archivo = destino / f"{paquete.run_id}-revision-{NUMERO_REVISION_INICIAL:03d}.json"
            archivo.write_text(paquete.model_dump_json(indent=2), encoding="utf-8")
            return archivo
        except OSError as exc:
            logger.error("paquete_no_conservado_en_disco", run_id=paquete.run_id, error=str(exc))
            return None

    async def _subir_a_oci(
        self, paquete: PaqueteSalida, periodo: str, run_id: str, request_id: str
    ) -> EvidenciaAlmacenamientoOCI:
        """Sube a OCI con comprobación SHA-256. Nunca lanza (M07)."""
        ruta_prevista = paquete.almacenamiento_oci.ruta_objeto
        try:
            resultado = await asyncio.to_thread(
                self.oci_client.upload_package,
                package_payload=paquete.model_dump(mode="json"),
                periodo_referencia=periodo,
                run_id=run_id,
                numero_revision=NUMERO_REVISION_INICIAL,
                request_id=request_id,
            )
            exito = resultado.status_almacenamiento == "guardado_con_exito"
            return EvidenciaAlmacenamientoOCI(
                bucket=resultado.bucket or self._bucket(),
                ruta_objeto=resultado.ruta_objeto or ruta_prevista,
                status_almacenamiento="guardado_con_exito" if exito else "guardado_error",
                comprobacion_lectura=bool(resultado.comprobacion_lectura) if exito else False,
            )
        except Exception as exc:  # noqa: BLE001 — aislamiento de fallos en OCI (M07)
            logger.error("oci_subida_fallida", run_id=run_id, request_id=request_id, error=str(exc))
            return EvidenciaAlmacenamientoOCI(
                bucket=self._bucket(),
                ruta_objeto=ruta_prevista,
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
            )

    async def _nodo_persistencia_dual(self, state: PipelineState) -> dict[str, Any]:
        analisis = state["analisis_cognitivo"]
        activos = state["activos_generados"]
        metadatos = state["metadatos_ejecucion"]
        if analisis is None or activos is None or metadatos is None:
            raise InvarianteViolada(
                "nodo_persistencia_dual requiere análisis, activos y metadatos; "
                "no se fabrica un PaqueteSalida"
            )
        run_id, request_id, periodo = state["run_id"], state["request_id"], state["periodo_referencia"]
        bound_log = self._log(state)

        # 1. PaqueteSalida (numero_revision = 1, estado = "pendiente").
        paquete = PaqueteSalida(
            run_id=run_id,
            request_id=request_id,
            metadatos_ejecucion=metadatos,
            analisis_resumido=analisis.resumen_comunidad,
            activos=activos,
            revision=EstadoRevision(numero_revision=NUMERO_REVISION_INICIAL, estado="pendiente"),
            almacenamiento_oci=EvidenciaAlmacenamientoOCI(
                bucket=self._bucket(),
                ruta_objeto=self._ruta_objeto(periodo, run_id),
                status_almacenamiento="no_iniciado",
                comprobacion_lectura=False,
            ),
        )

        # 2. SQLite: lote + mensajes_analizados + mensaje_temas en un bloque atómico.
        await asyncio.to_thread(
            self.db_manager.registrar_lote_y_analisis,
            analisis,
            state["interacciones_validas"],
            state["origen_comunidad"],
            periodo,
            len(state["interacciones_crudas"]),
            len(state["registros_rechazados"]),
        )
        await self._registrar_telemetria(state)

        # 3-4. OCI con comprobación de lectura; si falla, no se aborta (M07).
        evidencia = await self._subir_a_oci(paquete, periodo, run_id, request_id)
        if evidencia.status_almacenamiento != "guardado_con_exito":
            archivo = await asyncio.to_thread(self._conservar_en_disco, paquete)
            # Datos exactos que exige `retry_upload_from_disk()` para reintentar sin ambigüedad.
            bound_log.warning(
                "oci_guardado_error_paquete_conservado",
                ruta_local=str(archivo) if archivo else None,
                periodo_referencia=periodo,
                numero_revision=NUMERO_REVISION_INICIAL,
            )
        # `PaqueteSalida` no tiene `validate_assignment`: se reconstruye para revalidar la evidencia.
        paquete = PaqueteSalida.model_validate({**paquete.model_dump(), "almacenamiento_oci": evidencia})

        # Paquete de curaduría y evidencia de OCI (también `guardado_error`) en SQLite.
        await asyncio.to_thread(self.db_manager.registrar_paquete, paquete)

        self._transicion(state, "completado", status_oci=evidencia.status_almacenamiento)
        return {
            "paquete_salida": paquete,
            "almacenamiento_oci": evidencia,
            "estado_flujo": "completado",
        }

    # -- Nodos auxiliares ----------------------------------------------------------

    async def _nodo_finalizar_lote_vacio(self, state: PipelineState) -> dict[str, Any]:
        errores = list(state["errores"])
        if not errores:
            errores.append("lote sin registros válidos: ningún registro superó la validación")
        # Los rechazos ya quedaron en `auditoria_rechazados` (Nodo 1); aquí se
        # deja el lote registrado con `total_validos = 0`.
        await asyncio.to_thread(
            self.db_manager.registrar_lote,
            state["run_id"],
            state["request_id"] or DESCONOCIDO,
            state["origen_comunidad"] or DESCONOCIDO,
            state["periodo_referencia"] or DESCONOCIDO,
            len(state["interacciones_crudas"]),
            0,
            len(state["registros_rechazados"]),
        )
        self._transicion(state, "sin_registros_validos")
        return {
            "errores": errores,
            "paquete_salida": None,
            "estado_flujo": "sin_registros_validos",
        }

    async def _nodo_finalizar_sin_contenido(self, state: PipelineState) -> dict[str, Any]:
        analisis = state["analisis_cognitivo"]
        if analisis is None:
            raise InvarianteViolada("nodo_finalizar_sin_contenido requiere un análisis cognitivo")
        errores = list(state["errores"])

        # 1. Todos los mensajes analizados son no aptos.
        casos = [
            CasoNoPublicable.desde_mensaje_analizado(m)
            for m in analisis.mensajes_analizados
            if not m.apto_para_publicacion
        ]
        if not invariante_casos(analisis, casos):
            raise InvarianteViolada("casos_no_publicables no coincide con el conjunto de mensajes no aptos")

        # 2. Causa: ¿LLM no disponible (todo degradado) o el lote simplemente no califica?
        # `registros_rechazados >= len(mensajes)` es la regla de la spec; el analizador
        # reporta n-1 en una caída total, por eso se refuerza con las señales públicas
        # `tiene_analisis_utilizable` y `caida_total_llm`.
        telemetria = state["telemetria_analisis"]
        todo_degradado = (
            analisis.resumen_comunidad.registros_rechazados >= len(analisis.mensajes_analizados)
            or not tiene_analisis_utilizable(analisis)
            or bool(telemetria is not None and telemetria.caida_total_llm)
        )
        if todo_degradado:
            estado_final = "fallido"
            detalle = analisis.mensajes_analizados[0].motivo_seleccion
            errores.append(f"inferencia no disponible: {detalle}")
        else:
            estado_final = "sin_contenido_publicable"

        # 3. Lote y análisis a SQLite para que el curador pueda consultarlos.
        await asyncio.to_thread(
            self.db_manager.registrar_lote_y_analisis,
            analisis,
            state["interacciones_validas"],
            state["origen_comunidad"],
            state["periodo_referencia"],
            len(state["interacciones_crudas"]),
            len(state["registros_rechazados"]),
        )
        await self._registrar_telemetria(state)
        self._transicion(state, estado_final, casos_no_publicables=len(casos))
        return {
            "casos_no_publicables": casos,
            "errores": errores,
            "paquete_salida": None,
            "estado_flujo": estado_final,
        }

    # -- API pública ---------------------------------------------------------------

    def _estado_inicial(
        self,
        run_id: str,
        request_id: str = "",
        origen_comunidad: str = "",
        periodo_referencia: str = "",
        ruta_archivo_crudo: str | None = None,
        interacciones_crudas: list[dict[str, Any]] | None = None,
    ) -> PipelineState:
        return PipelineState(
            run_id=run_id,
            request_id=request_id,
            origen_comunidad=origen_comunidad,
            periodo_referencia=periodo_referencia,
            ruta_archivo_crudo=ruta_archivo_crudo,
            interacciones_crudas=interacciones_crudas or [],
            interacciones_validas=[],
            registros_rechazados=[],
            analisis_cognitivo=None,
            telemetria_analisis=None,
            telemetria_generacion=None,
            enfoque_editorial=None,
            conteo_enfoque_editorial=None,
            activos_generados=None,
            casos_no_publicables=[],
            metadatos_ejecucion=None,
            paquete_salida=None,
            almacenamiento_oci=None,
            errores=[],
            estado_flujo="iniciado",
        )

    def _a_resultado(self, final: dict[str, Any]) -> ResultadoPipeline:
        estado = final.get("estado_flujo")
        errores = list(final.get("errores", []))
        paquete = final.get("paquete_salida")
        if estado not in _ESTADOS_TERMINALES:
            errores.append(f"el grafo terminó en un estado no terminal: {estado!r}")
            estado, paquete = "fallido", None
        return ResultadoPipeline(
            estado_flujo=estado,
            run_id=final["run_id"],
            request_id=final.get("request_id") or DESCONOCIDO,
            registros_rechazados=[_a_registro_rechazado(r) for r in final.get("registros_rechazados", [])],
            casos_no_publicables=list(final.get("casos_no_publicables", [])),
            errores=errores,
            paquete_salida=paquete if estado == "completado" else None,
        )

    async def _resultado_fallido(self, inicial: PipelineState, exc: Exception) -> ResultadoPipeline:
        """Convierte una excepción técnica en `fallido`. Relee los rechazos por
        `run_id`, porque el estado intermedio no está disponible (sección 5.2)."""
        try:
            filas = await asyncio.to_thread(self.db_manager.leer_rechazados, inicial["run_id"])
        except Exception:  # noqa: BLE001 — el manejador de fallos nunca debe lanzar
            filas = []
        request_id = inicial["request_id"] or self._request_id_por_run.get(inicial["run_id"])
        return ResultadoPipeline(
            estado_flujo="fallido",
            run_id=inicial["run_id"],
            request_id=request_id or DESCONOCIDO,
            registros_rechazados=[_a_registro_rechazado(f) for f in filas],
            errores=[f"{type(exc).__name__}: {exc}"],
        )

    async def _ejecutar(self, inicial: PipelineState) -> ResultadoPipeline:
        bound_log = logger.bind(run_id=inicial["run_id"], request_id=inicial["request_id"] or DESCONOCIDO)
        try:
            final = await self.app.ainvoke(inicial)
            resultado = self._a_resultado(final)
        except Exception as exc:  # noqa: BLE001 — toda excepción técnica se devuelve como `fallido`
            bound_log.exception("pipeline_fallido", error=str(exc))
            resultado = await self._resultado_fallido(inicial, exc)
            bound_log = bound_log.bind(request_id=resultado.request_id)
        finally:
            self._request_id_por_run.pop(inicial["run_id"], None)
        try:
            # Estado terminal para consultas históricas (spec §8.8); nunca debe abortar el flujo.
            await asyncio.to_thread(
                self.db_manager.actualizar_estado_flujo, resultado.run_id, resultado.estado_flujo
            )
        except Exception as exc:  # noqa: BLE001
            bound_log.warning("estado_flujo_no_persistido", error=str(exc))
        bound_log.info(
            "pipeline_finalizado",
            estado_flujo=resultado.estado_flujo,
            hay_paquete=resultado.paquete_salida is not None,
            errores=len(resultado.errores),
        )
        return resultado

    async def ejecutar_desde_lote(self, lote: IngestionLote, run_id: str) -> ResultadoPipeline:
        """Ejecuta el pipeline a partir de un IngestionLote ya validado por
        FastAPI. Nunca lanza por un resultado de negocio: devuelve siempre un
        ResultadoPipeline; las excepciones técnicas se capturan y se
        devuelven como estado "fallido"."""
        inicial = self._estado_inicial(
            run_id=run_id,
            request_id=lote.request_id,
            origen_comunidad=lote.origen_comunidad,
            periodo_referencia=lote.periodo_referencia,
            interacciones_crudas=[i.model_dump(mode="json") for i in lote.interacciones],
        )
        return await self._ejecutar(inicial)

    async def ejecutar_desde_archivo(
        self,
        ruta_archivo: Path | str,
        run_id: str | None = None,
    ) -> ResultadoPipeline:
        """Punto de entrada para CLI, tests y lotes manuales en data/raw/.
        Puebla `ruta_archivo_crudo`; la lectura y el parseo ocurren dentro de
        `_nodo_validar_datos` (sección 4, Nodo 1, paso 1)."""
        ruta = Path(ruta_archivo)
        if run_id is None:
            run_id = ruta.stem if _NOMBRE_RUN.match(ruta.stem) else _generar_run_id()
        inicial = self._estado_inicial(run_id=run_id, ruta_archivo_crudo=str(ruta))
        return await self._ejecutar(inicial)
