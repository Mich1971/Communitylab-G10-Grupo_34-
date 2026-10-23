# Especificación Técnica y Plan de Implementación: `orchestration/pipeline_runner.py`

**Proyecto:** CommunityLab — Hackathon ONE G10 (Oracle Next Education & Alura) · Equipo 34

**Componente:** Orquestador de Pipeline y Máquina de Estados (LangGraph / Requisito R4 / Directivas M01, M02, M07)

**Responsables:** Enrique (Implementación / Orquestación) & Michael Chacón (AI Engineering)

**Fecha:** 23 de octubre de 2026 — versión 3.2

**Destinatario:** Squad de Desarrollo CommunityLab

**Estado del documento:** el orquestador ya está implementado. Esta versión deja de ser solo una especificación a futuro: describe el comportamiento real del código (`pipeline_runner.py`, `state.py`, `resultado.py`), lo contrasta con la v3.1 y actualiza el plan de fases. Las secciones marcadas **(nuevo en v3.2)** o **(actualizado en v3.2)** son las que cambian.

---

## 1. Resumen Ejecutivo y Propósito del Módulo

El archivo `orchestration/pipeline_runner.py` es el **cerebro orquestador** de CommunityLab. Implementado sobre **LangGraph (`>=1.2,<1.3`)**, coordina el flujo completo:

1. Recibir el lote desde el worker (o desde un llamador directo), validarlo con `data_pipeline/validador.py` y separar registros válidos de rechazados.
2. Invocar el análisis semántico en `ai_modules/cognitive_analyzer.py`.
3. Calcular el enfoque editorial del lote (`Logro`, `Duda`, `Dificultad`) según la directiva **M01**, como paso interno previo a la generación — no como bifurcación del grafo.
4. Generar los dos formatos de marketing (`PostLinkedIn` y `ResumenSemanal`) en `ai_modules/generador.py`, con la política de **cero hechos inventados** (verificación de `source_ids`), y adjuntar los **casos no publicables**.
5. Ensamblar el borrador inmutable inicial (`revision-001`, contrato canónico `PaqueteSalida`).
6. Ejecutar la persistencia dual: SQLite y OCI Object Storage con comprobación activa de lectura, cumpliendo la regla de oro **M07**.
7. **(nuevo en v3.2)** Registrar la telemetría de LLM por etapa y el estado terminal del flujo en SQLite.

**Principio de diseño.** El Contrato 3 (`PaqueteSalida`) solo se construye cuando existe material real para todos sus campos obligatorios. Por eso el orquestador **devuelve un `ResultadoPipeline`** (sección 5), que contiene el paquete únicamente si el flujo terminó en `completado`. Ningún camino fabrica un Contrato 3 vacío ni modifica `esquemas.py` para permitirlo.

Este módulo actúa como desacoplador: **ningún agente de IA escribe directamente a la base de datos ni a OCI**, y **la API de FastAPI no ejecuta inferencias bloqueantes**.

**Prerrequisitos de contrato (sección 9):**

* `esquemas.py` con `CasoNoPublicable` y `ActivosGenerados.casos_no_publicables` (parche aprobado por el equipo; `schema_version` se mantiene en `1.1.0`).
* `PuntuacionRelevancia.calcular_total` debe permanecer activo: el módulo cognitivo y los prompts dependen de que `total` se calcule solo.

## 2. Topología del Grafo de Estados en LangGraph

DAG con control de excepciones y **dos bifurcaciones condicionales**, ambas de control de flujo (nunca de formato de salida):

```
               [Inicio: lote en data/raw/{run_id}.json (vía worker) o IngestionLote]
                                     │
                                     ▼
                       ┌───────────────────────────┐
                       │    nodo_validar_datos     │
                       └─────────────┬─────────────┘
                                     │
               ┌─────────────────────┴──────────────────────┐
               │ ¿Existen registros válidos (len >= 1)?      │
               └─────────────────────┬──────────────────────┘
                         SÍ          │          NO (lote 100 % rechazado / archivo ilegible / sobre inválido)
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
                       │       nodo_generar_copys         │   (registra telemetría del análisis)
                       │ 1. Calcula enfoque_editorial      │
                       │    (solo orienta tono — M01)      │
                       │ 2. Casos no publicables           │
                       │    (determinista, sin LLM)        │
                       │ 3. Genera PostLinkedIn            │
                       │    + ResumenSemanal en paralelo   │
                       │ 4. Verifica invariantes y         │
                       │    reintenta a temperatura 0      │
                       │ 5. Acumula telemetría y compone   │
                       │    MetadatosEjecucion             │
                       └─────────────┬─────────────────────┘
                                     │
                                     ▼
                       ┌───────────────────────────┐
                       │   nodo_persistencia_dual  │
                       │ 1. Ensambla PaqueteSalida │
                       │ 2. SQLite: lote + análisis│
                       │ 3. SQLite: telemetría     │
                       │ 4. OCI Object Storage     │
                       │ 5. SQLite: paquete        │
                       └─────────────┬─────────────┘
                                     │
                                     ▼
                         [FIN — estado: completado]
```

Cualquier excepción no controlada la captura el método público (`ejecutar_desde_lote` / `ejecutar_desde_archivo`) y la convierte en un `ResultadoPipeline` con estado `fallido` (sección 5.2).

**Sobre la Regla de Oro M01.** El enrutamiento editorial no se modela como un nodo con aristas propias, porque M01 establece que *"el enrutamiento no bifurca el formato de salida; orienta los parámetros de tono y contexto"*. Las dos bifurcaciones deciden si existe material para generar contenido, no qué formato generar. Cuando hay material, siempre se generan ambos formatos.

## 3. Definición del Estado del Pipeline (`PipelineState`) *(actualizado en v3.2)*

El estado compartido se implementa con `typing.TypedDict` y vive en `orchestration/state.py`, desacoplado de `pipeline_runner.py` para evitar importaciones circulares.

```python
from typing import Any, Dict, List, Literal, Optional, TypedDict

from ai_modules.cognitive_analyzer import TelemetriaAnalisis
from ai_modules.generador import TelemetriaGeneracion
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
    telemetria_analisis: Optional[TelemetriaAnalisis]       # modelo interno de ai_modules/
    telemetria_generacion: Optional[TelemetriaGeneracion]   # NUEVO v3.2: acumulada, incluye reintentos

    # Enfoque editorial: estado interno de orquestación, no campos de un contrato.
    enfoque_editorial: Optional[Literal["Logro", "Duda", "Dificultad"]]
    conteo_enfoque_editorial: Optional[Dict[str, int]]

    # 4. Activos Generados (Contrato 3)
    activos_generados: Optional[ActivosGenerados]
    casos_no_publicables: List[CasoNoPublicable]
    metadatos_ejecucion: Optional[MetadatosEjecucion]

    # 5. Persistencia y Evidencia
    paquete_salida: Optional[PaqueteSalida]
    almacenamiento_oci: Optional[EvidenciaAlmacenamientoOCI]

    # 6. Control de Errores y Diagnóstico
    errores: List[str]
    estado_flujo: Literal[
        "iniciado", "validado", "analizado", "copys_generados",
        "completado",                # terminal: hay paquete
        "sin_registros_validos",     # terminal: sin paquete
        "sin_contenido_publicable",  # terminal: sin paquete
        "fallido",                   # terminal: sin paquete
    ]
```

**Por qué `telemetria_generacion` está en el estado.** LangGraph descarta las claves que el `TypedDict` no declara: sin el campo, la telemetría de la generación no llegaría al nodo de persistencia.

**Regla de uso:** `activos_generados` es `Optional` solo porque no existe hasta el Nodo 3. **Nunca** debe usarse para fabricar un `PaqueteSalida` cuando es `None`.

## 4. Especificación Detallada de los Nodos del Grafo *(actualizado en v3.2)*

### Nodo 1: `nodo_validar_datos`

* **Entrada:** `interacciones_crudas` o lectura de `ruta_archivo_crudo` (caso de `ejecutar_desde_archivo`).
* **Lógica:**
  1. Si `ruta_archivo_crudo` está definido y `interacciones_crudas` llega vacío, lee y parsea el JSON en un hilo (`asyncio.to_thread`). Un archivo inexistente, ilegible o con JSON mal formado se anota en `errores`.
  2. Si el payload no es un sobre `IngestionLote` (formato plano del jurado), aplica `adaptar_payload_oficial()`. La API ya adapta en el ingreso, así que los archivos que deposita llegan como sobre; esta rama protege los lotes manuales en `data/raw/`.
  3. Valida el sobre con `IngestionLote.model_validate`. Cualquier fallo (no es objeto, esquema inválido) equivale a "lote sin registros válidos": no se lanza excepción.
  4. Si hay lote, ejecuta `validar_lote_crudo()` y **registra de inmediato** el lote y los rechazados con `db_manager.registrar_lote(...)`, antes de cualquier paso que pueda fallar.
  5. Si no hay lote (paso 1 o 3 fallaron), registra igualmente una fila de `lotes_procesamiento` con ceros y valores `desconocido` en los campos de identidad que falten.
* **Salida del Estado:** `interacciones_validas`, `registros_rechazados`, `request_id`/`origen_comunidad`/`periodo_referencia` (del lote), `errores`, `estado_flujo = "validado"`.
* **Arista condicional (`_decidir_continuacion_datos`):** `"vacio"` si `len(interacciones_validas) == 0`; `"continuar"` en otro caso.

### Nodo 2: `nodo_analisis_cognitivo`

* **Entrada:** `interacciones_validas`, `run_id`, `request_id`.
* **Lógica:** llama a `analizar_lote_cognitivo_con_telemetria()` (no a `analizar_lote_cognitivo()`, que descarta la telemetría) y captura el `AnalisisCognitivo` junto con su `TelemetriaAnalisis`. Los mensajes que agotan los reintentos del LLM quedan **degradados** (puntuación 0, `apto_para_publicacion = false`, `requiere_soporte = true`) y no rompen el lote.
* **Salida del Estado:** `analisis_cognitivo`, `telemetria_analisis`, `estado_flujo = "analizado"`.
* **Arista condicional (`_decidir_tras_analisis`):** `"continuar"` si algún mensaje es apto; `"sin_contenido"` en otro caso.

### Nodo 3: `nodo_generar_copys`

* **Entrada:** `analisis_cognitivo` con al menos un mensaje apto (garantizado por la arista anterior). Si llegara `None`, lanza `InvarianteViolada` y el flujo termina en `fallido`.
* **Lógica:**
  1. **Enfoque editorial** (`calcular_enfoque_editorial`): cuenta los mensajes aptos por `categoria_enrutamiento` y elige la mayoritaria; en empate, prioridad `Logro` > `Dificultad` > `Duda` (orden a ratificar con el equipo editorial). El resultado y el conteo van al estado y al log estructurado. **M01:** orienta tono; nunca bifurca el formato.
  2. **Casos no publicables** (determinista, sin LLM): `CasoNoPublicable.desde_mensaje_analizado(m)` para cada mensaje no apto. Si `invariante_casos` falla, es un error de programación: `InvarianteViolada` → `fallido`.
  3. **Generación con reintentos.** Se invoca `generar_activos_marketing()` hasta `1 + MAX_REINTENTOS_INVARIANTES` veces (hoy 3 intentos). El primer intento usa la temperatura configurada; los reintentos usan `temperatura_cero=True`. Tras cada intento, `violaciones_trazabilidad()` revisa las invariantes 1 y 2 de la sección 7.3. Si tras el último intento persisten, lanza `InvarianteViolada` → `fallido`.
  4. **Ensamblado:** `ActivosGenerados(post_linkedin=..., resumen_semanal=..., casos_no_publicables=...)` se construye como instancia (no `model_copy(update=...)`, que omite validación).
  5. **Telemetría y metadatos (nuevo en v3.2):** `_sumar_telemetrias()` acumula **todos los intentos** de generación en una sola `TelemetriaGeneracion`, y `combinar_metadatos()` compone el único `MetadatosEjecucion` del paquete (sección 6).
* **Salida del Estado:** `enfoque_editorial`, `conteo_enfoque_editorial`, `activos_generados`, `casos_no_publicables`, `metadatos_ejecucion`, **`telemetria_generacion`**, `estado_flujo = "copys_generados"`.

### Nodo 4: `nodo_persistencia_dual`

* **Entrada:** análisis, activos y metadatos. Si falta alguno, lanza `InvarianteViolada`; no fabrica un paquete.
* **Orden real de las operaciones:**

  | Paso | Operación | Atomicidad |
  | --- | --- | --- |
  | 1 | Construye `PaqueteSalida` (`numero_revision = 1`, `estado = "pendiente"`, evidencia OCI en `no_iniciado`) | — |
  | 2 | `db_manager.registrar_lote_y_analisis(...)`: encabezado del lote, `mensajes_analizados`, `mensaje_temas`, `resumen_comunidad` | Una transacción |
  | 3 | **`_registrar_telemetria(state)`** (nuevo en v3.2) | Una transacción, best-effort |
  | 4 | `_subir_a_oci(...)`: `upload_package()` con comprobación SHA-256; nunca lanza | Externa |
  | 5 | Si OCI falló: `_conservar_en_disco()` y log de advertencia con `periodo_referencia` y `numero_revision` | — |
  | 6 | Reconstruye el paquete con la evidencia real (`PaqueteSalida.model_validate`, porque el modelo no tiene `validate_assignment`) | — |
  | 7 | `db_manager.registrar_paquete(paquete)`: paquete, activos, metadatos, revisión y evidencia OCI | Una transacción |

* **Cambio respecto a v3.1.** La v3.1 pedía un único bloque atómico para SQLite. En la implementación son **tres transacciones separadas** (pasos 2, 3 y 7), con la subida a OCI entre la 3 y la 7. Es una decisión consciente: el curador debe poder consultar el análisis aunque OCI o el guardado final fallen, y OCI no debe ejecutarse dentro de una transacción de SQLite. La consecuencia es que un corte entre pasos deja un lote con análisis pero sin paquete; el worker lo reintenta (sección 7.2).
* **Si OCI falla, el flujo no aborta (M07):** `status_almacenamiento = "guardado_error"`, `comprobacion_lectura = False`. El paquete queda en SQLite y una copia en `pending_oci/{run_id}-revision-001.json`, junto al archivo de la base. Si no se puede escribir la copia, se registra `paquete_no_conservado_en_disco`; el paquete sigue en SQLite.
* **Salida del Estado:** `paquete_salida`, `almacenamiento_oci`, `estado_flujo = "completado"`.

### Nodo auxiliar 1: `nodo_finalizar_lote_vacio`

* **Cuándo:** ningún registro superó la validación (lote 100 % corrupto, archivo ilegible, sobre inválido).
* **Lógica:** añade un motivo a `errores` si aún no hay uno, y vuelve a registrar el lote con `total_validos = 0`. Los rechazos ya quedaron en `auditoria_rechazados` (Nodo 1). **No invoca LLM, no genera paquete y no escribe en OCI.**
* **Salida:** `estado_flujo = "sin_registros_validos"`, `paquete_salida = None`.

### Nodo auxiliar 2: `nodo_finalizar_sin_contenido`

* **Cuándo:** hubo registros válidos y análisis, pero ningún mensaje es apto.
* **Lógica:**
  1. Construye `casos_no_publicables` con todos los mensajes (todos son no aptos) y verifica `invariante_casos`.
  2. **Distingue la causa.** El flujo termina en `fallido` si se cumple **cualquiera** de estas señales:
     * `registros_rechazados >= len(mensajes_analizados)`;
     * `not tiene_analisis_utilizable(analisis)`;
     * `telemetria_analisis.caida_total_llm`.

     El contrato fuerza `registros_validos >= 1`, así que una caída total se reporta como `registros_validos=1` y `registros_rechazados=n-1`; la primera señal sola no la detecta, por eso se combinan las tres. En ese caso se añade el error `"inferencia no disponible: ..."`. En otro caso, el modelo evaluó el lote y nada califica → `sin_contenido_publicable`.
  3. Registra el lote y el análisis en SQLite (`registrar_lote_y_analisis`) y **la telemetría del análisis** (nuevo en v3.2). **No genera `PaqueteSalida` ni escribe en OCI.**
* **Salida:** `estado_flujo` según la causa, `casos_no_publicables`, `paquete_salida = None`.

## 5. Firmas de Interfaz y API Pública del Módulo

### 5.1 Modelos de resultado (`orchestration/resultado.py`)

`ResultadoPipeline` **no es un contrato canónico**: es el valor de retorno del orquestador y vive en `orchestration/`.

```python
EstadoFlujoFinal = Literal[
    "completado", "sin_registros_validos", "sin_contenido_publicable", "fallido",
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
            raise ValueError("paquete_salida debe estar presente si y solo si estado_flujo == 'completado'")
        return self
```

El validador hace imposible, por construcción, un `completado` sin paquete o un paquete adjunto a un estado sin contenido.

### 5.2 Clase pública y ciclo de ejecución *(actualizado en v3.2)*

```python
class CommunityLabPipeline:
    def __init__(self, oci_client: OCIStorageClient | None = None,
                 db_manager: LocalDatabaseManager | None = None) -> None: ...

    async def ejecutar_desde_lote(self, lote: IngestionLote, run_id: str) -> ResultadoPipeline: ...
    async def ejecutar_desde_archivo(self, ruta_archivo: Path | str, run_id: str | None = None) -> ResultadoPipeline: ...
```

**Ciclo común (`_ejecutar`):**

1. `await self.app.ainvoke(estado_inicial)`; el estado final se convierte con `_a_resultado()`. Si el grafo termina en un estado no terminal, el resultado es `fallido` con el error `"el grafo terminó en un estado no terminal: ..."`.
2. Cualquier excepción técnica se captura, se registra con `run_id` y `request_id`, y se devuelve como `fallido` mediante `_resultado_fallido()`. Como el estado intermedio no está disponible tras una excepción, ese método **relee los rechazos por `run_id`** con `db_manager.leer_rechazados()` y recupera el `request_id` desde un mapa interno (`_request_id_por_run`) poblado por el Nodo 1.
3. **(nuevo en v3.2)** Siempre, incluso tras un fallo, se escribe el estado terminal con `db_manager.actualizar_estado_flujo(run_id, estado)` (columna `lotes_procesamiento.estado_flujo`, valor por defecto `'iniciado'`). Es best-effort: si falla, se registra `estado_flujo_no_persistido` y el resultado no cambia.
4. Se emite el log `pipeline_finalizado` con estado, presencia de paquete y número de errores.

**`run_id`.** `ejecutar_desde_archivo` reutiliza el nombre del archivo si cumple `run-YYYYMMDDTHHMMSSZ-<12 hex>` (los que genera la API) y genera uno nuevo en otro caso. `ejecutar_desde_lote` exige que el llamador lo entregue.

**Contrato de los métodos públicos:**

* Son `async def`; no se usa `app.invoke()` dentro del bucle de eventos.
* Nunca lanzan por un resultado de negocio; las excepciones técnicas se devuelven como `fallido`.
* Todo acceso a SQLite y OCI (síncronos) pasa por `asyncio.to_thread`.

### 5.3 Estados terminales y su reflejo en la API

El runner no responde HTTP. El worker copia `estado_flujo` a `solicitudes_ingesta.estado`, y el cliente lo lee con `GET /api/v1/runs/{run_id}`.

| `estado_flujo` | Significado | `paquete_salida` | Persistencia | Archivo del lote |
| --- | --- | --- | --- | --- |
| `completado` | Flujo completo; borrador `revision-001` listo para curaduría | Presente | SQLite + OCI + telemetría | `data/processed/` |
| `completado` con `status_almacenamiento = "guardado_error"` | Paquete generado; falló solo OCI (M07) | Presente | SQLite + `pending_oci/` + telemetría | `data/processed/` |
| `sin_registros_validos` | Ningún registro superó la validación | `null` | `lotes_procesamiento` + `auditoria_rechazados` | `data/processed/` |
| `sin_contenido_publicable` | Hubo análisis, pero ningún mensaje es apto | `null` (con `casos_no_publicables`) | `lotes_procesamiento` + `mensajes_analizados` + telemetría del análisis | `data/processed/` |
| `fallido` | Todos los mensajes degradados, o excepción no controlada | `null` | Lo ya persistido (con telemetría del análisis si hubo caída total) | `data/failed/` |

Estados intermedios del registro de la API: `recibiendo` → `en_cola` → `procesando`.

**El cliente decide según el `estado` del cuerpo del `GET`, no según el código HTTP.** Un reenvío del mismo `request_id` con el mismo contenido nunca reprocesa: devuelve `200` con el `run_id` existente. El motivo del fallo queda en `ResultadoPipeline.errores` y en los logs; `solicitudes_ingesta` no tiene columna de motivo (decisión pendiente, sección 10).

### 5.4 Contrato con la API, el worker y la base de datos *(actualizado en v3.2)*

| Pieza | Responsabilidad | Archivo |
| --- | --- | --- |
| API | Autentica, valida el sobre, aplica idempotencia (`request_id` + SHA-256), adapta el formato plano del jurado, deposita `data/raw/{run_id}.json` de forma atómica y responde `202` (`409` si el contenido cambia). No ejecuta inferencia. | `api/main.py`, `api/registro.py` |
| Worker | Reclama archivos de `data/raw/` hacia `data/processing/`, llama a `ejecutar_desde_archivo(ruta, run_id)`, copia `estado_flujo` al registro y archiva el lote. Un solo worker por `data_dir`. Al arrancar reencola lo que quedó en `processing/`. **Preflight de arranque:** verifica que el gestor de base de datos tenga los cinco métodos que usa el orquestador y, si falta alguno, aborta con un mensaje claro en vez de dejar que todos los lotes terminen en `fallido`. | `orchestration/worker.py` |
| Orquestador | Ejecuta el grafo y devuelve `ResultadoPipeline`. Debe tolerar reejecuciones por `run_id` (sección 7.2). | `orchestration/pipeline_runner.py` |
| Gestor SQLite | Expone los métodos que consume el orquestador (tabla siguiente). | `storage/local_db.py` |

**Métodos de `LocalDatabaseManager` que usa el orquestador:**

| Método | Quién lo llama | ¿Lo exige el preflight del worker? |
| --- | --- | --- |
| `registrar_lote(...)` | Nodo 1, nodo vacío | Sí |
| `registrar_lote_y_analisis(...)` | Nodo 4, nodo sin contenido | Sí |
| `registrar_paquete(paquete)` | Nodo 4 | Sí |
| `leer_rechazados(run_id)` | `_resultado_fallido` | Sí |
| `actualizar_estado_flujo(run_id, estado)` | `_ejecutar` | Sí |
| **`registrar_telemetria(run_id, request_id, registros)`** (nuevo en v3.2) | `_registrar_telemetria` | **No** (a propósito: es métrica operativa y su ausencia no debe impedir el arranque) |

El orquestador lee `data/raw/` solo a través del worker; no vigila la carpeta por su cuenta.

## 6. Telemetría y `MetadatosEjecucion` *(nuevo en v3.2)*

### 6.1 Telemetría por etapa en SQLite

`_registrar_telemetria(state)` guarda **una fila por etapa** en `telemetria_llamadas_llm`: la del análisis (`TelemetriaAnalisis`) y, si existe, la de la generación (`TelemetriaGeneracion`).

* **Cuándo se escribe:** en `nodo_persistencia_dual` (análisis + generación) y en `nodo_finalizar_sin_contenido` (solo análisis), siempre **después** de `registrar_lote_y_analisis` para respetar la clave foránea hacia `lotes_procesamiento`.
* **Semántica de reemplazo:** `registrar_telemetria` borra las filas del `run_id` y las vuelve a insertar. Por eso el orquestador siempre envía la **lista completa** de etapas y una reejecución no duplica filas.
* **Best-effort:** cualquier excepción (incluido un gestor sin el método) se registra como `telemetria_no_persistida` y no cambia el estado del flujo ni el paquete.
* **Reintentos de generación:** los intentos que violaron una invariante se **suman** en la fila de generación (latencia, tokens de entrada, salida y cacheados), de modo que la tabla refleja el costo real del run.
* **Columnas utilizadas:** `proveedor`, `modelo`, `latencia_ms`, `prompt_tokens`, `completion_tokens`, `tokens_totales`, `tokens_cacheados`, `fecha_evento`; `source_id` queda en `NULL` porque la cifra es agregada por lote.

**Limitaciones conocidas:**

1. La tabla **no tiene columna `etapa`**. Las dos filas se distinguen por `modelo` y por orden de inserción; si ambas etapas usan el mismo proveedor y modelo, `vista_telemetria_lote` (agrupa por `run_id`, `proveedor`, `modelo`) las fusiona en una sola fila. Añadir `etapa TEXT` requiere un cambio de esquema y una migración (decisión pendiente, sección 10).
2. Si el Nodo 3 falla (por ejemplo, invariante violada tras los reintentos), la telemetría del análisis **no se persiste**, porque se escribe después de guardar el lote en la fase de persistencia.
3. Las cifras de análisis tienen el alcance documentado en `TelemetriaAnalisis`: `latencia_ms` es el tiempo de pared del lote completo (las inferencias corren en paralelo) y los tokens cuentan solo respuestas exitosas.

### 6.2 Composición de `MetadatosEjecucion`

`combinar_metadatos(telemetria_analisis, telemetria_generacion)` compone el único `MetadatosEjecucion` del paquete:

* `modelo = "<modelo análisis> + <modelo generación>"` y `prompt_version` igual, combinadas con `" + "`;
* `latencia_ms` y `tokens_totales`: suma de ambas etapas;
* si no hay telemetría de generación, se usan solo los datos del análisis.

La composición implementa la propuesta de la v3.1 (análisis y generación). **Sigue pendiente de confirmación del equipo** que el paquete deba registrar ambas etapas y no solo una.

## 7. Manejo de Errores, Idempotencia y Políticas de Resiliencia

### 7.1 Trazabilidad y aislamiento de fallos

1. **Trazabilidad.** `run_id` y `request_id` se vinculan a cada log de `structlog`; tras calcular el enfoque editorial se añaden `enfoque_editorial` y `conteo_enfoque_editorial`. Cada transición de `estado_flujo` emite un evento `transicion_estado_flujo`.
2. **Aislamiento de fallos en OCI (M07).** Si falla la conexión o las credenciales, el pipeline no aborta: asigna `guardado_error`, conserva el paquete en SQLite y en `pending_oci/`, y el estado final sigue siendo `completado`. Para recuperar, hay dos vías: `OCIStorageClient.retry_upload_from_disk()` con el archivo de `pending_oci/`, o `local_db.sincronizar_pendientes_oci()`, que reconstruye el paquete desde SQLite y lo sube con la evidencia en `no_iniciado` (el cliente OCI exige que ese campo no llegue precompuesto). Si la subida y la lectura de verificación son correctas, la base se actualiza a `guardado_con_exito`.
3. **Telemetría y estado terminal** son best-effort y nunca abortan el flujo (secciones 5.2 y 6.1).
4. **Mensajes degradados dentro de un lote exitoso.** Un mensaje degradado tiene `apto_para_publicacion = false`, así que aparece en `casos_no_publicables` con su motivo. Es el comportamiento deseado: el curador lo ve y puede reintentarlo.

### 7.2 Idempotencia por `run_id` *(actualizado en v3.2)*

El worker puede reejecutar un lote tras una caída. Qué ocurre en cada capa:

| Capa | Comportamiento ante una reejecución del mismo `run_id` |
| --- | --- |
| `registrar_lote`, `registrar_lote_y_analisis` | Upsert del encabezado y del resumen; los mensajes se actualizan por la clave compuesta `(run_id, source_id)`, y los temas, entidades y rechazos de origen `analisis` se reescriben. No duplica. |
| `registrar_paquete` | Borra y reinserta la revisión `(run_id, numero_revision)`; las tablas hijas se limpian por `ON DELETE CASCADE`. |
| `registrar_telemetria` | Reemplaza las filas del run. |
| `actualizar_estado_flujo` | Sobrescribe con el nuevo estado terminal. |
| **OCI `upload_package`** | **Riesgo conocido** (abajo). |

**Riesgo conocido: colisión en OCI al reejecutar un run que ya había subido su paquete.** El orquestador vuelve a llamar al LLM, de modo que el nuevo paquete tiene contenido y `fecha_generacion` distintos. La ruta `activos/{periodo}/{run_id}/revision-001.json` ya existe; OCI responde 412 y el cliente compara hashes. Como difieren, lo trata como colisión real y devuelve `guardado_error` (comportamiento correcto del cliente: la inmutabilidad no se negocia). El paquete nuevo se conserva en SQLite y en `pending_oci/`, pero un reintento posterior volvería a chocar con el mismo objeto. Esta conclusión se deriva del código del cliente y de `PaqueteSalida` (`fecha_generacion` con `default_factory`); no se ha reproducido con una caída real entre la subida y el archivado. **Opciones para decidir con el equipo:** (a) que el orquestador compruebe si la ruta ya existe y, en ese caso, reutilice el paquete almacenado o use `numero_revision` siguiente; (b) aceptar el `guardado_error` y dejar la resolución al curador. El caso solo se da si el proceso cae entre la subida a OCI y el archivado del lote, una ventana corta.

### 7.3 Validación preventiva de trazabilidad factual

Antes de persistir, el orquestador verifica tres invariantes. Sea $M$ el conjunto de `mensajes_analizados`, $A = \{\, m.\text{source\_id} \mid m \in M,\ m.\text{apto\_para\_publicacion} = \text{true} \,\}$ y $N = \{\, m.\text{source\_id} \mid m \in M,\ m.\text{apto\_para\_publicacion} = \text{false} \,\}$.

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

Las dos primeras las comprueba `violaciones_trazabilidad()` (también rechaza listas vacías e identificadores en blanco) y, si fallan, fuerzan el reintento a temperatura cero hasta agotar `MAX_REINTENTOS_INVARIANTES`. La tercera la comprueba `invariante_casos()` y es determinista: si falla, es un error de programación y el flujo termina en `fallido`.

## 8. Pruebas y Verificación *(nuevo en v3.2)*

| Suite | Qué cubre |
| --- | --- |
| `tests/test_pipeline_telemetria.py` (7 pruebas) | Orquestador real con SQLite real y `OCIStorageClient` real sobre un backend en memoria; solo se simulan las llamadas al LLM. Cubre: dos filas por etapa en `completado`; acumulación de reintentos de generación; solo análisis en `sin_contenido_publicable`; telemetría en caída total del LLM; reejecución sin duplicados; fallo de telemetría que no cambia el resultado; gestor sin el método. |
| `tests/test_local_db.py` | Cinco métodos del orquestador, `estado_flujo` (incluida la migración de una base sin la columna), `registrar_telemetria`, sincronización con OCI. |
| `tests/test_worker.py` | Candado, archivado resiliente, carga del ejecutor y preflight de métodos. |
| `tests/test_oci_client.py` | Inmutabilidad, reintento idempotente, colisión, precedencia de credenciales. |

Estado de la suite completa en el entorno de integración: **255 pruebas aprobadas, 1 omitida, 2 de integración deseleccionadas** (requieren credenciales OCI). La omitida es una prueba del worker que comprobaba que `pipeline_runner` aún no existía.

Para cada estado terminal de la sección 5.3 se ejecutó el flujo completo con el worker, el orquestador y el gestor real de SQLite: `completado`, `sin_contenido_publicable`, `fallido` (LLM caído en todo el lote) y `sin_registros_validos`. En los cuatro casos el `estado_flujo` quedó escrito en la base y el lote en el directorio esperado.

**Pendiente:** `tests/test_pipeline_runner.py` con la matriz de la Fase 4 (lote de 4 registros: 3 válidos y 1 rechazado, y un caso por estado terminal) debe confirmarse con Enrique; las pruebas de esta versión cubren la telemetría y la integración con el gestor, no sustituyen esa matriz.

## 9. Plan de Implementación por Fases *(actualizado en v3.2)*

| Fase | Denominación | Entregable verificable | Estado |
| --- | --- | --- | --- |
| **Fase 0** | Contrato y prerrequisitos | Parche de `esquemas.py` (`CasoNoPublicable`, `casos_no_publicables`) aprobado; columna `estado_flujo` en `lotes_procesamiento` | Parche aprobado por el equipo; **falta integrarlo en `main`**. Columna implementada con migración. |
| **Fase 1** | Estructura, estado y resultado | `orchestration/state.py` y `orchestration/resultado.py` | Implementadas (`state.py` ampliado con `telemetria_generacion`). |
| **Fase 2** | Construcción de nodos | `pipeline_runner.py` con 4 nodos, 2 auxiliares y 2 aristas condicionales | Implementada. Depende de que `ai_modules/generador.py` esté integrado. |
| **Fase 3** | Persistencia dual e integración OCI | `storage/local_db.py` y `storage/oci_client.py` con SHA-256 | Implementada: gestor con los métodos del orquestador, `estado_flujo` y telemetría. |
| **Fase 4** | Pruebas de integración y mocks | `tests/test_pipeline_runner.py`: un caso por estado terminal | **Parcial.** Hay pruebas de telemetría e integración (sección 8); falta confirmar la matriz de estados. |
| **Fase 5** | Worker, API y Streamlit | `api/main.py`, `orchestration/worker.py` conectados con `ejecutar_desde_archivo`; Streamlit con progreso en vivo | API y worker implementados; el worker carga el orquestador real y valida el gestor al arrancar. **Falta Streamlit** (`ui/app.py`) y la vista de casos no publicables. |
| **Fase 6** *(nueva)* | Endurecimiento previo al Demo Day (27 de octubre) | Decisiones de la sección 10 resueltas; riesgo de colisión en OCI (7.2) cerrado; prueba con credenciales OCI reales (`pytest -m integration`) | Pendiente. |

## 10. Observaciones, Decisiones Pendientes y Recomendaciones para el Squad

**Decisiones pendientes del equipo**

1. **Composición de `MetadatosEjecucion`.** Hoy suma ambas etapas (sección 6.2). Confirmar o cambiar a "solo análisis" o "solo copys".
2. **Columna `etapa` en `telemetria_llamadas_llm`.** Sin ella, las filas de análisis y generación no se distinguen de forma fiable (6.1, limitación 1). Requiere migración de Andrea.
3. **Columna `motivo` en `solicitudes_ingesta`.** Sin ella, n8n no puede distinguir un fallo reintentable (LLM caído) de uno definitivo; el motivo solo está en `errores` y en los logs.
4. **Colisión en OCI al reejecutar un run** (7.2): opción (a) o (b).
5. **Persistencia de una caída total del LLM.** Cuando el run termina `fallido` por caída total, el análisis degradado y un `resumen_comunidad` con `registros_validos = 1` (valor forzado por el contrato) quedan en la base. Un panel que lea esas tablas sin filtrar por `lotes_procesamiento.estado_flujo` podría mostrarlas como un resumen exitoso. Recomendación: filtrar las vistas por estado, o no persistir el resumen en `fallido`.
6. **`schema_version`.** El parche de `esquemas.py` es aditivo y compatible. Subir `PaqueteSalida.schema_version` (hoy `"1.1.0"`) no está aprobado; si se aprueba, hay que actualizar el valor fijo en `storage/oci_client.py` y los documentos de arquitectura.
7. **Prioridad de empate del enfoque editorial** (`Logro` > `Dificultad` > `Duda`): ratificar con el equipo editorial.
8. **`resumen_comunidad.registros_validos`:** el contrato (`ResumenComunidad`) y el `CHECK` de la tabla exigen `>= 1`. Es la causa de que una caída total del LLM se reporte como `registros_validos = 1` (punto 5). Decidir si ambos deben admitir `>= 0`; cambiarlo implica tocar el contrato y la tabla a la vez.

**Recomendaciones de implementación**

9. **Sin importaciones circulares.** `PipelineState` y `ResultadoPipeline` viven en `state.py` y `resultado.py`.
10. **Ejecución asíncrona pura.** Todo método del orquestador es `async def`; el trabajo síncrono (SQLite, OCI, lectura de archivos) va en `asyncio.to_thread`.
11. **Respeto a M01.** Las dos bifurcaciones son de control de flujo; nunca se emite "solo un post" o "solo una FAQ".
12. **Persistir la justificación del enfoque editorial.** Ningún campo canónico registra por qué se eligió el enfoque; mientras no haya consenso, `conteo_enfoque_editorial` queda en el log estructurado.
13. **Ruta de revisión humana (`numero_revision = 2`).** Decidir si vive en `api/main.py` / `ui/app.py` o en un método adicional de `CommunityLabPipeline`.
14. **Casos no publicables en la base.** No hace falta tabla nueva: basta una vista sobre `mensajes_analizados` con `apto_para_publicacion = 0`.
15. **No sobrescribir contadores.** `lotes_procesamiento.total_rechazados` cuenta rechazos del validador; `ResumenComunidad.registros_rechazados` cuenta mensajes degradados por el módulo cognitivo. Son medidas distintas.
16. **Pruebas sin consumo de saldo.** La suite `pytest -m "not integration"` debe pasar sin red. Las pruebas del orquestador simulan solo el LLM; OCI se reemplaza por un backend en memoria.
17. **Secretos.** Las credenciales de OCI se entregan por persona (claves de API individuales) y nunca por Discord ni Trello.

## 11. Cambios respecto a versiones anteriores

### Cambios de la versión 3.2 respecto a la 3.1

| Tema | v3.1 | v3.2 |
| --- | --- | --- |
| Estado del módulo | Especificación | Orquestador implementado; el documento describe el código real |
| `PipelineState` | Sin telemetría de generación | Campo `telemetria_generacion` (LangGraph descarta claves no declaradas) |
| Generación | "Reintentar con temperatura cero" | 1 intento + `MAX_REINTENTOS_INVARIANTES` reintentos; `InvarianteViolada` → `fallido`; telemetría acumulada de todos los intentos |
| `MetadatosEjecucion` | Decisión pendiente | `combinar_metadatos` implementa la suma de ambas etapas; pendiente de confirmar |
| Persistencia en SQLite | "Un bloque atómico" | Tres transacciones separadas (lote+análisis, telemetría, paquete), con OCI entre medio |
| Telemetría | No persistida | Una fila por etapa en `telemetria_llamadas_llm`, best-effort y con semántica de reemplazo |
| Estado terminal en SQLite | Recomendación | `lotes_procesamiento.estado_flujo`, escrito siempre al final de `_ejecutar` (también en `fallido`) |
| Detección de caída total del LLM | `tiene_analisis_utilizable` | Tres señales combinadas (contadores, `tiene_analisis_utilizable`, `caida_total_llm`) |
| Lote ilegible o sobre inválido | Desvío a lote vacío | Igual, más registro del lote con valores `desconocido` y ceros |
| Fallo técnico | Capturado en la API pública | Capturado en `_ejecutar`; releer rechazos por `run_id`; recuperación del `request_id` desde mapa interno |
| Recuperación de OCI | `retry_upload_from_disk()` | Además, `sincronizar_pendientes_oci()` desde SQLite; copia en `pending_oci/` |
| Worker | Contrato descrito | Preflight de los cinco métodos del gestor; `registrar_telemetria` queda fuera a propósito |
| Idempotencia | "Debe tolerar reejecuciones" | Tabla por capa y riesgo conocido de colisión en OCI |
| Plan | Fases 0–5 | Estados reales y Fase 6 (endurecimiento antes del Demo Day) |

### Cambios de la versión 3 respecto a la 2

| Tema | v2 | v3 |
| --- | --- | --- |
| Valor de retorno | `PaqueteSalida` en todos los caminos | `ResultadoPipeline`; el paquete solo existe si el estado es `completado` |
| Lote vacío | "`PaqueteSalida` mínimo sin activos" (incompatible con el contrato) | Sin paquete; estado `sin_registros_validos` |
| Ningún mensaje apto | No contemplado (`PostLinkedIn` exige `source_ids ≥ 1`) | Segunda bifurcación y nodo auxiliar; estado `sin_contenido_publicable` o `fallido` |
| Estados | 6 | 4 terminales + 4 intermedios |
| Nodos auxiliares | 1 | 2 |
| Casos no publicables | No existían | Campo del Contrato 3, determinista, con su invariante |
| Resultado hacia el cliente (v3.1) | `estado_http()` con 200/503/500 | `POST` solo ingesta (`202`); estado por `GET /runs/{run_id}` |
