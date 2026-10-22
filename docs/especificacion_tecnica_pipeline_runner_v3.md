# Especificación Técnica y Plan de Implementación: `orchestration/pipeline_runner.py`

**Proyecto:** CommunityLab — Hackathon ONE G10 (Oracle Next Education & Alura) · Equipo 34

**Componente:** Orquestador de Pipeline y Máquina de Estados (LangGraph / Requisito R4 / Directivas M01, M02, M07)

**Responsables:** Enrique (Implementación / Orquestación) & Michael Chacón (AI Engineering)

**Fecha:** 18 de octubre de 2026 — versión 3.1

**Destinatario:** Squad de Desarrollo CommunityLab

---

## 1. Resumen Ejecutivo y Propósito del Módulo

El archivo `orchestration/pipeline_runner.py` constituye el **cerebro orquestador** de CommunityLab. Implementado sobre **LangGraph (`>=1.2,<1.3`)**, coordina el flujo de ejecución completo:

1. Recibir los datos desde `data_pipeline/validador.py` y segregar registros válidos de rechazados.
2. Invocar el análisis semántico en `ai_modules/cognitive_analyzer.py`.
3. Calcular el enfoque editorial del lote (`Logro`, `Duda`, `Dificultad`) según la directiva **M01**, como paso interno previo a la generación de contenido — no como bifurcación del grafo.
4. Generar los dos formatos base de marketing (`PostLinkedIn` y `ResumenSemanal`) en `ai_modules/generador.py`, con la política de **cero hechos inventados** (enlace forzado de `source_ids`), y adjuntar los **casos no publicables** del lote.
5. Ensamblar el borrador inmutable inicial (`revision-001.json`, contrato canónico `PaqueteSalida`).
6. Ejecutar la persistencia dual: SQLite (`data/communitylab.db`) y OCI Object Storage con comprobación activa de lectura, cumpliendo la regla de oro **M07**.

**Principio de diseño de esta versión.** El Contrato 3 (`PaqueteSalida`) solo puede construirse cuando existe material real para todos sus campos obligatorios. Por eso el orquestador **no devuelve un `PaqueteSalida` en todos los caminos**: devuelve un `ResultadoPipeline` (sección 5), que contiene el paquete únicamente cuando el flujo se completó. Ningún camino fabrica un Contrato 3 vacío ni modifica `esquemas.py` para permitirlo.

Este módulo actúa como desacoplador universal: **ningún agente de IA escribe directamente a la base de datos ni a OCI**, y **la API de FastAPI no ejecuta inferencias bloqueantes en memoria**.

**Prerrequisitos de contrato (ver sección 7):**

* `esquemas.py` con `CasoNoPublicable` y `ActivosGenerados.casos_no_publicables` (parche aprobado por el equipo).
* `PuntuacionRelevancia.calcular_total` debe permanecer activo: el módulo cognitivo y los prompts dependen de que `total` se calcule solo.

## 2. Topología del Grafo de Estados en LangGraph

DAG con control de excepciones y **dos bifurcaciones condicionales**, ambas de control de flujo (nunca de formato de salida):

```
               [Inicio: Evento Lote / data/raw/{run_id}.json]
                                     │
                                     ▼
                       ┌───────────────────────────┐
                       │    nodo_validar_datos     │
                       └─────────────┬─────────────┘
                                     │
               ┌─────────────────────┴──────────────────────┐
               │ ¿Existen registros válidos (len >= 1)?      │
               └─────────────────────┬──────────────────────┘
                         SÍ          │          NO (lote 100 % rechazado / archivo ilegible)
                                     │          └──────> [nodo_finalizar_lote_vacio] ──> FIN
                                     ▼                     estado: sin_registros_validos
                       ┌───────────────────────────┐
                       │  nodo_analisis_cognitivo  │
                       └─────────────┬─────────────┘
                                     │
               ┌─────────────────────┴──────────────────────┐
               │ ¿Hay al menos un mensaje                    │
               │  apto_para_publicacion = true?              │
               └─────────────────────┬──────────────────────┘
                         SÍ          │          NO
                                     │          └──────> [nodo_finalizar_sin_contenido] ──> FIN
                                     ▼                     estado: sin_contenido_publicable
                       ┌─────────────────────────────────┐   o fallido (si todo fue degradado)
                       │       nodo_generar_copys         │
                       │ 1. Calcula enfoque_editorial      │
                       │    (solo orienta tono — M01)      │
                       │ 2. Genera PostLinkedIn            │
                       │    + ResumenSemanal en paralelo   │
                       │ 3. Construye casos_no_publicables │
                       │    (determinista, sin LLM)        │
                       └─────────────┬─────────────────────┘
                                     │
                                     ▼
                       ┌───────────────────────────┐
                       │   nodo_persistencia_dual  │
                       │ 1. Ensambla PaqueteSalida │
                       │ 2. SQLite local           │
                       │ 3. OCI Object Storage SDK │
                       └─────────────┬─────────────┘
                                     │
                                     ▼
                         [FIN — estado: completado]
```

Cualquier excepción no controlada durante la ejecución la captura el método público (`ejecutar_desde_lote` / `ejecutar_desde_archivo`) y la convierte en un `ResultadoPipeline` con estado `fallido` (sección 5.2).

**Sobre la Regla de Oro M01.** El enrutamiento editorial no se modela como un nodo con aristas propias, porque M01 establece que *"el enrutamiento no bifurca el formato de salida; orienta los parámetros de tono y contexto"*. Las dos bifurcaciones de arriba **no** contradicen M01: deciden si existe material para generar contenido, no qué formato generar. Cuando hay material, siempre se generan ambos formatos.

## 3. Definición del Estado del Pipeline (`PipelineState`)

El estado compartido entre nodos se implementa con `typing.TypedDict` y vive en `orchestration/state.py` (ver sección 8, punto 1):

```python
from typing import Any, Dict, List, Literal, Optional, TypedDict

from ai_modules.cognitive_analyzer import TelemetriaAnalisis
from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    CasoNoPublicable,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MetadatosEjecucion,
    PaqueteSalida,
)


class PipelineState(TypedDict):
    """Estado global transferido entre nodos del grafo de LangGraph."""

    # 1. Identificadores Operativos e Idempotencia
    run_id: str
    request_id: str
    origen_comunidad: str
    periodo_referencia: str
    ruta_archivo_crudo: Optional[str]

    # 2. Datos en Tránsito y Auditoría
    interacciones_crudas: List[Dict[str, Any]]
    interacciones_validas: List[InteraccionValidada]
    registros_rechazados: List[Dict[str, Any]]  # filas de auditoria_rechazados

    # 3. Análisis Cognitivo (Contrato 2)
    analisis_cognitivo: Optional[AnalisisCognitivo]
    # Telemetría del análisis (modelo, prompt_version, latencia y tokens).
    # Modelo interno de ai_modules/cognitive_analyzer.py, no un contrato.
    telemetria_analisis: Optional[TelemetriaAnalisis]

    # Enfoque editorial y el conteo que lo sustenta: se calculan dentro de
    # nodo_generar_copys (sección 4, Nodo 3). Son estado interno de
    # orquestación, no campos de un contrato canónico.
    enfoque_editorial: Optional[Literal["Logro", "Duda", "Dificultad"]]
    conteo_enfoque_editorial: Optional[Dict[str, int]]

    # 4. Activos Generados (Contrato 3)
    # `casos_no_publicables` viaja dentro de `activos_generados`, pero también
    # se conserva aparte para poder devolverlo en los caminos sin paquete.
    activos_generados: Optional[ActivosGenerados]
    casos_no_publicables: List[CasoNoPublicable]
    metadatos_ejecucion: Optional[MetadatosEjecucion]

    # 5. Persistencia y Evidencia
    paquete_salida: Optional[PaqueteSalida]
    almacenamiento_oci: Optional[EvidenciaAlmacenamientoOCI]

    # 6. Control de Errores y Diagnóstico
    errores: List[str]
    estado_flujo: Literal[
        "iniciado",
        "validado",
        "analizado",
        "copys_generados",
        "completado",              # terminal: hay paquete
        "sin_registros_validos",   # terminal: sin paquete (lote vacío / 100 % rechazado)
        "sin_contenido_publicable",# terminal: sin paquete (ningún mensaje apto)
        "fallido",                 # terminal: sin paquete (error técnico)
    ]
```

**Regla de uso:** `activos_generados` es `Optional` solo porque no existe hasta el Nodo 3. **Nunca** debe usarse para fabricar un `PaqueteSalida` cuando es `None`.

## 4. Especificación Detallada de los Nodos del Grafo

### Nodo 1: `nodo_validar_datos`

* **Entrada:** `state["interacciones_crudas"]` o lectura de `state["ruta_archivo_crudo"]`.
* **Lógica:**
  1. Si `ruta_archivo_crudo` está definido y `interacciones_crudas` llega vacío (caso de `ejecutar_desde_archivo`), leer y parsear el JSON de esa ruta como primer paso. Un archivo inexistente o con JSON mal formado se registra en `errores` y desvía el flujo igual que un lote sin registros válidos.
  2. Si `interacciones_crudas` proviene en formato plano del jurado, ejecutar `adaptar_payload_oficial()`.
  3. Invocar `data_pipeline.validador.validar_lote_crudo()` para segregar `validos` de `rechazados`.
  4. Registrar de inmediato los rechazados en la tabla `auditoria_rechazados` de SQLite (los diccionarios que devuelve el validador usan exactamente las columnas de esa tabla).
* **Salida del Estado:** `interacciones_validas`, `registros_rechazados`, `estado_flujo = "validado"`.
* **Arista condicional (`_decidir_continuacion_datos`):** `"vacio"` si `len(interacciones_validas) == 0`; `"continuar"` en otro caso.

### Nodo 2: `nodo_analisis_cognitivo`

* **Entrada:** `interacciones_validas`, `run_id`, `request_id`.
* **Lógica:** llama a `ai_modules.cognitive_analyzer.analizar_lote_cognitivo_con_telemetria()` (no a `analizar_lote_cognitivo()`, que descarta la telemetría) y captura el `AnalisisCognitivo` (Contrato 2) junto con su `TelemetriaAnalisis`. Los mensajes que agotan los reintentos del LLM quedan **degradados** (puntuación 0, `apto_para_publicacion = false`, `requiere_soporte = true`) y no rompen el lote.
* **Salida del Estado:** `analisis_cognitivo`, `telemetria_analisis`, `estado_flujo = "analizado"`.
* **Arista condicional (`decidir_tras_analisis`):** `"continuar"` si algún mensaje tiene `apto_para_publicacion = true`; `"sin_contenido"` en otro caso.

### Nodo 3: `nodo_generar_copys`

* **Entrada:** `analisis_cognitivo` (con al menos un mensaje apto, garantizado por la arista anterior).
* **Lógica:**
  1. **Enfoque editorial (paso interno):** sobre los mensajes con `apto_para_publicacion = true`, cuenta cuántos caen en cada `categoria_enrutamiento` y elige la mayoritaria; en empate, prioridad `Logro` > `Dificultad` > `Duda` (orden a ratificar con el equipo editorial). Resultado y conteo se escriben en `state["enfoque_editorial"]` y `state["conteo_enfoque_editorial"]` y se vinculan al log estructurado.
     * **M01:** orienta tono y contexto; nunca bifurca el formato. Aunque el lote sea predominantemente `Duda` o `Dificultad`, se generan ambos formatos.
  2. Invoca `ai_modules.generador.generar_activos_marketing()` pasando `enfoque_editorial` como parámetro de tono.
  3. Genera `PostLinkedIn` y `ResumenSemanal` simultáneamente.
  4. **Casos no publicables (determinista, sin LLM):** construye la lista con
     `CasoNoPublicable.desde_mensaje_analizado(m)` para cada mensaje con `apto_para_publicacion = false`. La lista **nunca** proviene de texto generado por un modelo.
  5. Ensambla `ActivosGenerados(post_linkedin=..., resumen_semanal=..., casos_no_publicables=...)` construyendo la instancia (no con `model_copy(update=...)`, que omite validación).
  6. **Invariantes** (sección 6.3): cero hechos inventados para ambos activos y coherencia de `casos_no_publicables`. Si se viola alguno, se rechaza el activo y se reintenta el prompt con temperatura cero.
* **Salida del Estado:** `enfoque_editorial`, `conteo_enfoque_editorial`, `activos_generados`, `casos_no_publicables`, `metadatos_ejecucion`, `estado_flujo = "copys_generados"`.

### Nodo 4: `nodo_persistencia_dual`

* **Entrada:** estado consolidado con análisis y activos.
* **Lógica:**
  1. Construye el `PaqueteSalida` (`numero_revision = 1`, `estado = "pendiente"`).
  2. **SQLite** (transacción atómica, `with connection:`): inserta en `lotes_procesamiento`, `mensajes_analizados` y `mensaje_temas`.
  3. **OCI:** `OCIStorageClient.upload_package()` con payload JSON, `periodo_referencia`, `run_id`, `numero_revision = 1` y `request_id`; comprobación de lectura SHA-256; el resultado se inyecta en `paquete_salida.almacenamiento_oci`.
  4. Si OCI falla, el flujo **no aborta** (M07): `status_almacenamiento = "guardado_error"`, `comprobacion_lectura = False`, paquete conservado en SQLite y disco.
* **Salida del Estado:** `paquete_salida`, `almacenamiento_oci`, `estado_flujo = "completado"`.

### Nodo auxiliar 1: `nodo_finalizar_lote_vacio`

* **Cuándo:** ningún registro superó la validación (lote 100 % corrupto, archivo ilegible).
* **Lógica:** registra el lote en SQLite (`lotes_procesamiento` con `total_validos = 0`; los rechazos ya quedaron en `auditoria_rechazados`), añade el motivo a `errores` y emite el log. **No invoca LLM, no genera paquete y no escribe en OCI.**
* **Salida del Estado:** `estado_flujo = "sin_registros_validos"`, `paquete_salida = None`.

### Nodo auxiliar 2: `nodo_finalizar_sin_contenido`

* **Cuándo:** hubo registros válidos y análisis, pero ningún mensaje es apto para publicación.
* **Lógica:**
  1. Construye `casos_no_publicables` con todos los mensajes analizados (todos son no aptos).
  2. Distingue la causa con campos públicos del contrato:
     * si `resumen_comunidad.registros_rechazados >= len(mensajes_analizados)`, **todos** los mensajes fueron degradados: la causa es la indisponibilidad del LLM → `estado_flujo = "fallido"` con el error `"inferencia no disponible: ..."`;
     * en otro caso, el modelo evaluó el lote y nada califica → `estado_flujo = "sin_contenido_publicable"`.
  3. Registra el lote y el análisis en SQLite (los mensajes analizados sí se persisten, para que el curador pueda consultarlos). **No genera `PaqueteSalida` ni escribe en OCI.**
* **Salida del Estado:** `estado_flujo` (según la causa), `casos_no_publicables`, `paquete_salida = None`.

## 5. Firmas de Interfaz y API Pública del Módulo

### 5.1 Modelos de resultado (`orchestration/resultado.py`)

`ResultadoPipeline` **no es un contrato canónico**: es el valor de retorno del orquestador. Vive en `orchestration/`, no en `esquemas.py`. Código verificado contra los contratos vigentes:

```python
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from esquemas import CasoNoPublicable, PaqueteSalida

EstadoFlujoFinal = Literal[
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
]


class RegistroRechazado(BaseModel):
    """Espejo tipado de una fila de `auditoria_rechazados` (validador.py)."""

    mensaje_id: Optional[str] = None
    autor: Optional[str] = None
    canal: Optional[str] = None
    error_detalle: str


class ResultadoPipeline(BaseModel):
    estado_flujo: EstadoFlujoFinal
    run_id: str
    request_id: str
    registros_rechazados: List[RegistroRechazado] = Field(default_factory=list)
    casos_no_publicables: List[CasoNoPublicable] = Field(default_factory=list)
    errores: List[str] = Field(default_factory=list)
    paquete_salida: Optional[PaqueteSalida] = None

    @model_validator(mode="after")
    def paquete_solo_si_completado(self) -> "ResultadoPipeline":
        if (self.estado_flujo == "completado") != (self.paquete_salida is not None):
            raise ValueError(
                "paquete_salida debe estar presente si y solo si estado_flujo == 'completado'"
            )
        return self
```

El validador hace imposible, por construcción, un resultado `completado` sin paquete o un paquete adjunto a un estado sin contenido.

### 5.2 Clase pública y firmas

```python
from pathlib import Path
from typing import Optional

from langgraph.graph import StateGraph, END
import structlog

from esquemas import IngestionLote
from orchestration.resultado import ResultadoPipeline
from orchestration.state import PipelineState
from data_pipeline.validador import validar_lote_crudo, adaptar_payload_oficial
from ai_modules.cognitive_analyzer import analizar_lote_cognitivo
from ai_modules.generador import generar_activos_marketing
from storage.oci_client import OCIStorageClient
from storage.local_db import LocalDatabaseManager

logger = structlog.get_logger("orchestration.pipeline_runner")


class CommunityLabPipeline:
    """Coordinador del grafo de ejecución LangGraph para CommunityLab."""

    def __init__(
        self,
        oci_client: Optional[OCIStorageClient] = None,
        db_manager: Optional[LocalDatabaseManager] = None,
    ) -> None:
        self.oci_client = oci_client or OCIStorageClient()
        self.db_manager = db_manager or LocalDatabaseManager()
        self.workflow = self._construir_grafo()
        self.app = self.workflow.compile()

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

    async def ejecutar_desde_lote(
        self,
        lote: IngestionLote,
        run_id: str,
    ) -> ResultadoPipeline:
        """Ejecuta el pipeline a partir de un IngestionLote ya validado por
        FastAPI. Nunca lanza por un resultado de negocio: devuelve siempre un
        ResultadoPipeline; las excepciones técnicas se capturan y se
        devuelven como estado "fallido"."""
        ...

    async def ejecutar_desde_archivo(
        self,
        ruta_archivo: Path | str,
        run_id: Optional[str] = None,
    ) -> ResultadoPipeline:
        """Punto de entrada para CLI, tests y lotes manuales en data/raw/.
        Puebla `ruta_archivo_crudo`; la lectura y el parseo ocurren dentro de
        `_nodo_validar_datos` (sección 4, Nodo 1, paso 1)."""
        ...
```

**Contrato de los métodos públicos:**

* Se implementan con `async def` y se consumen con `await self.app.ainvoke(initial_state)`. No se usa `self.app.invoke()` dentro del bucle de eventos de FastAPI.
* Un `try/except` alrededor de `ainvoke` convierte cualquier excepción en `ResultadoPipeline(estado_flujo="fallido", errores=[...])` y emite el log con `run_id` y `request_id`.
* Limitación conocida: si la excepción interrumpe `ainvoke`, el estado intermedio no está disponible. Por eso los rechazos se persisten en SQLite en el Nodo 1, antes de cualquier paso que pueda fallar, y el manejador de `fallido` puede releerlos por `run_id`.

### 5.3 Estados terminales y su reflejo en la API

Cada estado terminal se corresponde con exactamente un valor de `ResultadoPipeline.estado_flujo`. **El runner no responde HTTP**: la API (`api/main.py`) solo ingesta y contesta `202` (ver 5.4). El worker (`orchestration/worker.py`) copia `estado_flujo` a `solicitudes_ingesta.estado`, y el cliente (n8n, Streamlit) lo lee con `GET /api/v1/runs/{run_id}`, que responde `200` con ese estado.

| `estado_flujo` | Significado | `paquete_salida` | Persistencia | Archivo del lote |
| --- | --- | --- | --- | --- |
| `completado` | Flujo completo; borrador `revision-001` listo para curaduría | Presente | SQLite + OCI | `data/processed/` |
| `completado` con `status_almacenamiento = "guardado_error"` | Paquete generado; falló solo la subida a OCI (M07) | Presente | SQLite + disco | `data/processed/` |
| `sin_registros_validos` | Ningún registro superó la validación | `null` | `lotes_procesamiento` + `auditoria_rechazados` | `data/processed/` |
| `sin_contenido_publicable` | Hubo análisis, pero ningún mensaje es apto | `null` (con `casos_no_publicables`) | `lotes_procesamiento` + `mensajes_analizados` | `data/processed/` |
| `fallido` | Todos los mensajes degradados, o excepción no controlada | `null` | Lo ya persistido | `data/failed/` |

Estados intermedios del registro, escritos por la API y el worker: `recibiendo` → `en_cola` → `procesando`.

**Qué cambia respecto a v3.0:** desaparece `estado_http()` y el mapeo 200/503/500. Un `POST` ya no espera al pipeline, así que no puede devolver su resultado. El motivo del fallo (LLM no disponible u otro) queda en `ResultadoPipeline.errores` y en los logs; `solicitudes_ingesta` no tiene columna de motivo. Si n8n necesita distinguir fallos reintentables, hay que añadir una columna `motivo` (decisión del equipo).

**El cliente decide según el `estado` del cuerpo del `GET`, no según el código HTTP.** Un reenvío del mismo `request_id` con el mismo contenido nunca reprocesa: devuelve `200` con el `run_id` existente.

### 5.4 Contrato con la API y el worker

| Pieza | Responsabilidad | Archivo |
| --- | --- | --- |
| API | Autentica, valida el sobre `IngestionLote`, aplica idempotencia (`request_id` + SHA-256), deposita `data/raw/{run_id}.json` de forma atómica, responde `202` (`409` si el contenido cambia). No ejecuta inferencia. | `api/main.py`, `api/registro.py` |
| Worker | Reclama archivos de `data/raw/` con `os.rename` a `data/processing/`, llama a `ejecutar_desde_archivo(ruta, run_id)`, copia `estado_flujo` al registro y archiva el lote. Un solo worker por `data_dir` (candado `flock`). Al arrancar reencola lo que quedó en `processing/`. | `orchestration/worker.py` |
| Orquestador | Ejecuta el grafo y devuelve `ResultadoPipeline`. **Debe ser idempotente por `run_id`** (el worker puede reejecutar un lote tras una caída): los inserts en SQLite usan `run_id` como clave y OCI ya resuelve el reintento con `if_none_match` más comparación de hash. | `orchestration/pipeline_runner.py` |

El orquestador lee `data/raw/` solo a través del worker; no debe vigilar la carpeta por su cuenta. El archivo contiene el `IngestionLote` completo (incluye `request_id`, `origen_comunidad` y `periodo_referencia`).

## 6. Manejo de Errores, Idempotencia y Políticas de Resiliencia

1. **Idempotencia y Trazabilidad.** `run_id` y `request_id` se vinculan en todo el ciclo y en cada log de `structlog`:

   ```python
   bound_log = logger.bind(run_id=state["run_id"], request_id=state["request_id"])
   # ...tras calcular el enfoque editorial en nodo_generar_copys:
   bound_log = bound_log.bind(
       enfoque_editorial=state["enfoque_editorial"],
       conteo_enfoque_editorial=state["conteo_enfoque_editorial"],
   )
   ```

2. **Aislamiento de Fallos en OCI (M07).** Si falla la conexión o las credenciales de OCI, el pipeline no aborta: asigna `status_almacenamiento = "guardado_error"` y `comprobacion_lectura = False`, registra el incidente en SQLite y conserva el paquete localmente para reintentar con `retry_upload_from_disk()`. El estado final sigue siendo `completado`.

3. **Validación Preventiva de Trazabilidad Factual.** Antes de persistir, el orquestador verifica tres invariantes, porque no todo mensaje procesado es apto para difusión pública. Sea $M$ el conjunto de `mensajes_analizados`, $A = \{\, m.\text{source\_id} \mid m \in M,\ m.\text{apto\_para\_publicacion} = \text{true} \,\}$ y $N = \{\, m.\text{source\_id} \mid m \in M,\ m.\text{apto\_para\_publicacion} = \text{false} \,\}$.

   `PostLinkedIn` solo puede citar mensajes aptos:

   $$
   \forall\, s \in \text{source\_ids}(\text{post\_linkedin}),\quad s \in A
   $$

   `ResumenSemanal` puede citar cualquier mensaje procesado:

   $$
   \forall\, s \in \text{source\_ids}(\text{resumen\_semanal}),\quad s \in \{\, m.\text{source\_id} \mid m \in M \,\}
   $$

   `casos_no_publicables` debe ser exactamente el conjunto de no aptos, sin duplicados:

   $$
   \{\, c.\text{source\_id} \mid c \in \text{casos\_no\_publicables} \,\} = N \quad\text{y}\quad \text{sin repetidos}
   $$

   Si el generador de copy emite un identificador inexistente, vacío, o un `source_id` de `PostLinkedIn` fuera de $A$, el nodo rechaza el activo y fuerza un reintento del prompt con temperatura cero. La tercera condición es determinista: si falla, es un error de programación (no del LLM) y el flujo termina en `fallido`.

   ```python
   def invariante_casos(analisis, casos) -> bool:
       no_aptos = {m.source_id for m in analisis.mensajes_analizados if not m.apto_para_publicacion}
       ids = [c.source_id for c in casos]
       return len(ids) == len(set(ids)) and set(ids) == no_aptos
   ```

4. **Mensajes degradados dentro de un lote exitoso.** Un mensaje degradado por el módulo cognitivo tiene `apto_para_publicacion = false`, así que aparece en `casos_no_publicables` con su motivo de degradación. Es el comportamiento deseado: el curador lo ve y puede reintentarlo.

## 7. Plan de Implementación por Fases (Fases Restantes)

| Fase | Denominación | Entregable Verificable | Dependencias |
| --- | --- | --- | --- |
| **Fase 0** | Contrato y prerrequisitos | Parche de `esquemas.py` (`CasoNoPublicable`, `casos_no_publicables`) **aprobado por el equipo**; falta integrarlo en `main`; PR #20 de `cognitive_analyzer.py` integrado; columna `estado_flujo` en `lotes_procesamiento` (ver sección 8, punto 8) | Consenso del equipo |
| **Fase 1** | Estructura, estado y resultado | `orchestration/state.py` y `orchestration/resultado.py` con sus pruebas | `esquemas.py` (Fase 0) |
| **Fase 2** | Construcción de Nodos | `orchestration/pipeline_runner.py` con los 4 nodos principales, los 2 nodos auxiliares y las 2 aristas condicionales | `ai_modules/cognitive_analyzer.py`, `ai_modules/generador.py` |
| **Fase 3** | Persistencia Dual e Integración OCI | Conexión con `storage/local_db.py` y `storage/oci_client.py` (hash SHA-256) | `storage/oci_client.py` (listo); `storage/local_db.py` (a confirmar) |
| **Fase 4** | Pruebas de Integración y Mocks | `tests/test_pipeline_runner.py`: lote de 4 registros (3 válidos, 1 rechazado) y **un caso por cada estado terminal de la sección 5.3** | Suite `pytest` |
| **Fase 5** | Worker, API y Streamlit | `api/main.py` y `orchestration/worker.py` **ya implementados y probados** con ejecutor simulado; falta conectarlos con `CommunityLabPipeline.ejecutar_desde_archivo`. Streamlit con progreso en vivo (lee `GET /runs/{run_id}` o `solicitudes_ingesta`) y vista de casos no publicables | `ui/app.py`, Fase 2 |

## 8. Observaciones y Recomendaciones para la Implementación (Squad)

1. **Evitar importaciones circulares.** `PipelineState` y `ResultadoPipeline` van en archivos desacoplados (`orchestration/state.py`, `orchestration/resultado.py`), no dentro de `pipeline_runner.py`.

2. **Ejecución asíncrona pura.** Todo método del orquestador es `async def` y se consume con `await app.ainvoke(...)`. No se usan métodos síncronos bloqueantes dentro del bucle de eventos.

3. **Transaccionalidad en SQLite.** En `nodo_persistencia_dual`, las inserciones en `lotes_procesamiento`, `mensajes_analizados` y `mensaje_temas` van en un bloque atómico (`with connection:`). Los nodos auxiliares usan el mismo patrón para su escritura.

4. **Respeto a M01 (generación simultánea).** Cuando hay material publicable, el generador invoca ambos formatos. Las dos bifurcaciones de la sección 2 son de control de flujo y no emiten "solo una FAQ" ni "solo un post".

5. **Persistir la justificación del enfoque editorial.** Ningún campo canónico registra por qué se eligió el enfoque. Mientras no haya consenso para agregar uno, `conteo_enfoque_editorial` queda vinculado al log estructurado.

6. **Visibilidad de progreso (Streamlit y `structlog`).** Emitir un evento de log por cada transición de `estado_flujo`, para que Streamlit pueda hacer *polling* sobre `solicitudes_ingesta` (vía `GET /runs/{run_id}`) o sobre los logs. La ruta de revisión humana (`numero_revision = 2`) debe decidirse explícitamente: en `api/main.py` / `ui/app.py` o con un método adicional en `CommunityLabPipeline`.

7. **Testing rápido sin consumo de saldo.** `tests/test_pipeline_runner.py` usa fixtures con mocks sobre `CognitiveAgentFactory` y `OCIStorageClient`. La suite `pytest -m "not integration"` debe pasar sin red y en menos de 2 segundos. Cubrir cada estado terminal con un mock que lo provoque (por ejemplo, un agente que siempre falla produce `fallido`/503).

8. **Registrar el estado terminal en SQLite.** `lotes_procesamiento` no tiene hoy una columna para el estado del flujo. Se recomienda agregar `estado_flujo TEXT NOT NULL` en el diseño de la base, para que Streamlit distinga un lote vacío de uno en curso o fallido. Es un cambio del diseño SQLite, no del contrato JSON. Nota: el estado en vivo ya lo lleva `solicitudes_ingesta.estado` (tabla de la API); `lotes_procesamiento.estado_flujo` solo duplicaría el estado terminal para consultas históricas.

9. **Casos no publicables en la base de datos.** No hace falta una tabla nueva: los datos ya están en `mensajes_analizados`. Basta una vista sobre `apto_para_publicacion = 0` para la consulta del panel, evitando duplicar información.

10. **No sobrescribir contadores.** `lotes_procesamiento.total_rechazados` cuenta los rechazos del validador, mientras `ResumenComunidad.registros_rechazados` cuenta los mensajes degradados por el módulo cognitivo. Son medidas distintas y cada una debe alimentar su propio campo.

11. **Composición de `MetadatosEjecucion` (decisión pendiente).** El módulo cognitivo ya expone sus cuatro campos mediante `telemetria_analisis.a_metadatos_ejecucion()` (`modelo`, `prompt_version`, `latencia_ms`, `tokens_totales`). Como el paquete lleva un único `MetadatosEjecucion` y los copys también consumen tokens, el equipo debe decidir si el paquete registra solo el análisis, solo los copys o ambos. Propuesta: sumar `latencia_ms` y `tokens_totales`, y combinar `modelo` y `prompt_version` de ambas etapas (por ejemplo, `"gpt-4o-mini + gpt-4o-mini"`). Cifras a tener en cuenta: `latencia_ms` del análisis es el tiempo de pared del lote (las inferencias corren en paralelo, no se suman) y los tokens cuentan solo respuestas exitosas, por lo que los reintentos fallidos no figuran.

12. **Versión del esquema.** El parche de `esquemas.py` es aditivo y compatible con los JSON existentes. Subir `PaqueteSalida.schema_version` (hoy `"1.1.0"`) es una decisión del equipo; si se aprueba, hay que actualizar también el valor fijo en `storage/oci_client.py` y los documentos de arquitectura.

## 9. Cambios respecto a la versión 2

| Tema | v2 | v3 |
| --- | --- | --- |
| Valor de retorno | `PaqueteSalida` en todos los caminos | `ResultadoPipeline`; el paquete solo existe si el estado es `completado` |
| Lote vacío | "`PaqueteSalida` mínimo sin activos" (incompatible con el contrato) | Sin paquete; estado `sin_registros_validos` |
| Ningún mensaje apto | No contemplado (`PostLinkedIn` exige `source_ids ≥ 1`) | Segunda bifurcación y nodo auxiliar; estado `sin_contenido_publicable` o `fallido` |
| Estados | 6 | 4 terminales + 4 intermedios, con reflejo en el registro de la API (v3.1) |
| Nodos auxiliares | 1 | 2 |
| Casos no publicables | No existían | Campo nuevo del Contrato 3, construido de forma determinista, con su invariante |
| Dependencia del módulo cognitivo | "Bloqueada" hasta que resolviera su resiliencia | Resiliencia implementada; pendiente la integración del PR #20 |

### Cambios de la versión 3.1 respecto a la 3.0

| Tema | v3.0 | v3.1 |
| --- | --- | --- |
| Resultado hacia el cliente | El endpoint delegaba al runner y devolvía 200/503/500 con `estado_http()` | El `POST` solo ingesta (`202`); el estado se lee con `GET /runs/{run_id}`. `estado_http()` eliminado |
| Quién invoca al runner | La API | El worker local (`orchestration/worker.py`) |
| Idempotencia | `run_id` como clave en SQLite | Además, registro de la API (`request_id` + SHA-256); el orquestador debe tolerar reejecuciones por `run_id` |
| Fase 5 | Pendiente | API y worker implementados; falta la integración con el orquestador |
