# Backend, Agentes Cognitivos y Persistencia — Notas para Agentes

Este archivo rige el desarrollo de los módulos de procesamiento, IA, orquestación y almacenamiento de CommunityLab (`api/`, `data_pipeline/`, `ai_modules/`, `orchestration/`, `storage/`). Lee primero el archivo maestro [../AGENTS.md](../AGENTS.md).

## 1. Stack Técnico del Backend

* **Intérprete:** Python 3.11 gestionado mediante `uv`.

* **API Framework:** FastAPI con Uvicorn (modo asíncrono para rutas I/O).

* **Contratos Canónicos:** Pydantic v2 en `esquemas.py` (`IngestionLote`, `InteraccionValidada`, `AnalisisCognitivo`, `PaqueteSalida`).

* **Agentes de IA:** PydanticAI con soporte multi-proveedor desacoplado (`OpenAIModel`, `GeminiModel`, `AnthropicModel`).

* **Grafo de Orquestación:** LangGraph para control de estados, bifurcaciones de enrutamiento y reintentos automáticos.

* **Observabilidad:** `structlog` en formato JSON estructurado con context binding (`run_id`, `request_id`).

* **Persistencia:** SQLite local para auditoría (`db_sqlite.py`) y OCI Object Storage SDK (`oci`) para archivado inmutable.

## 2. Responsabilidades por Módulo

### 2.1 Ingesta y Transporte (`api/`)

* **Endpoint Canónico:** `POST /api/v1/interactions/process`.

* **Autenticación:** Valida la cabecera `X-Webhook-Secret` contra `COMMUNITYLAB_WEBHOOK_SECRET` usando comparación en tiempo constante (`secrets.compare_digest`).

* **Idempotencia:** Verifica el par (`request_id`, hash SHA-256 del cuerpo). Ante una petición repetida con idéntico payload, responde con el `run_id` existente y código HTTP 200. Si el payload difiere, responde HTTP 409 (`Conflict`).

* **Escritura Atómica:** Escribe primero en `data/raw/temp_{run_id}.json` y ejecuta `os.replace` hacia `data/raw/{run_id}.json`. Retorna HTTP 202 (`Accepted`) sin procesar la inferencia en el hilo de la API.

### 2.2 Validación de Datos y Auditoría Local (`data_pipeline/`)

* **Validación Registro a Registro:** Toma `IngestionLote` y procesa cada registro individualmente:

  * Si el texto está vacío tras sanitizar o carece de campos obligatorios, lo inserta en la tabla `auditoria_descartes` de SQLite con su motivo de rechazo.

  * Si el registro es válido, lo transforma en `InteraccionValidada`.

* El lote continúa hacia la IA si contiene al menos un mensaje válido (`min_length=1`). Si todos son inválidos, genera el resumen correspondiente y finaliza sin invocar llamadas costosas a LLMs.

### 2.3 Agente de Análisis Cognitivo (`ai_modules/cognitive_analyzer.py`)

* **Sin Estado (*Stateless*):** No mantiene memoria conversacional ni base de datos vectorial (RAG descartado por alcance del MVP).

* **Clasificación Estricta:**

  * `sentimiento`: `Literal["Altamente Positivo", "Positivo", "Neutro", "Dificultad/Negativo"]`.

  * `categoria_enrutamiento`: `Literal["Logro", "Duda", "Dificultad"]`.

* **Cálculo Determinista del Score (0 a 6 puntos):**

  * Dimensiones: `evidencia_explicita` (0–2), `utilidad_comunitaria` (0–2), `claridad_contexto` (0–2).

  * El LLM produce las dimensiones y el validador Pydantic (`@model_validator(mode="after")`) calcula automáticamente `total = suma(dimensiones)` para evitar deslices aritméticos del modelo.

* **Justificación Obligatoria:** `motivo_seleccion` debe contener un mínimo de 10 caracteres fundamentando la elección.

* **Aislamiento de IDs:** El LLM **no** inventa `run_id` ni `request_id`; el wrapper de Python inyecta los identificadores heredados al instanciar el contrato `AnalisisCognitivo`.

### 2.4 Generación de Copys (`ai_modules/copy_generator.py`)

* Redacta simultáneamente:

  1. `PostLinkedIn`: Título, cuerpo estructurado, hashtags sugeridos y lista de `source_ids`.

  2. `ResumenSemanal`: Titular, resumen sintético y lista de `source_ids`.

* **Invariante Crítico:** La lista `source_ids` no puede estar vacía. Debe mapear exactamente a los `id` de las interacciones utilizadas.

### 2.5 Orquestación y Estados (`orchestration/pipeline_runner.py`)

* LangGraph coordina la secuencia: `validar_datos -> analizar_cognitivo -> enrutar_editorial -> generar_copys -> sincronizar_borrador_oci`.

* **Resiliencia Operativa:** Aplica reintentos con *exponential backoff with jitter* ante errores HTTP 429 o 5xx del LLM.

* **Degradación Elegante:** Si se agotan los reintentos para un mensaje, fija `puntuacion_relevancia.total = 0`, `apto_para_publicacion = False`, conserva el `source_id` original y documenta el incidente en `motivo_seleccion`.

### 2.6 Cliente OCI Object Storage (`storage/oci_client.py`)

* **Nomenclatura Inmutable:** Guarda exclusivamente bajo la ruta:

  ```
  activos/{periodo_referencia}/{run_id}/revision-{numero_revision:03d}.json
  
  ```

* **Comprobación Activa de Lectura (Regla M07):** Tras `put_object`, realiza `get_object`, compara el hash SHA-256 byte a byte y asigna por software:

  ```
  status_almacenamiento = "guardado_con_exito"
  comprobacion_lectura = True
  
  ```

* Si la lectura falla, marca `guardado_error` y permite reintentos desde disco (`retry_upload_from_disk`) sin incurrir en nuevas llamadas a los modelos.

## 3. Convenciones de Código y Tipado

* **Tipado Estricto:** Anota parámetros y retornos en todas las funciones públicas.

* **Manejo de Tiempos:** Usa marcas de tiempo UTC con zona horaria explícita (`datetime.now(timezone.utc)`). Prohibido el uso de `datetime.utcnow()`.

* **Observabilidad Estructurada:** Registra eventos con `structlog`:

  ```
  logger.info(
      "analisis_cognitivo_finalizado",
      run_id=run_id,
      mensajes_procesados=len(mensajes),
      latencia_ms=latencia,
      proveedor=proveedor_actual,
  )
  
  ```

## 4. Estrategia de Pruebas con Pytest

* **Pruebas Rápidas Unitarias:** Deben ejecutarse sin conexión de red ni dependencias de credenciales activas (`pytest -m "not integration"`).

* **Mocks en Límites Externos:** Simula las respuestas del SDK de OCI y los endpoints de LLMs en las pruebas unitarias de pipeline.

* **Prueba de Primera Integración (S0):** `tests/test_primera_prueba.py` debe validar el lote de 4 registros (3 válidos, 1 con texto vacío rechazado a auditoría).

* **Pruebas de Integración:** Marcadas con `@pytest.mark.integration`, evalúan llamadas reales multi-proveedor o persistencia efectiva en el bucket Always Free.

## 5. Anti-Patrones Rechazados en Backend

* Bifurcar el flujo condicional para emitir únicamente un post de LinkedIn O una FAQ (viola la directiva M01).

* Ejecutar inferencia síncrona o bloqueante en el endpoint de la API.

* Modificar `esquemas.py` sin consenso formal de los “usuarios” responsables.

* Enmascarar excepciones generales con bloques `except Exception: pass`.

* Sobrescribir archivos JSON de revisión previa en el bucket de OCI.