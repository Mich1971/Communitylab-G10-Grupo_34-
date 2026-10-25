# Plan de implementación: consultas de curaduría y revisiones

Oct 24, 2026 · @Michael Chacon

Para que el panel de curaduría funcione contra datos reales antes del Demo Day (27 de octubre), el backend añade 8 consultas de lectura, una operación transaccional de revisiones y un servicio de guardado en OCI, expuestos con la misma interfaz en Python y en HTTP. No cambia contratos, `schema_version` ni tablas.

## 1. Punto de partida

Sergio acepta las seis consultas que pidió, la operación transaccional de revisiones con revisión base, el servicio de guardado en OCI y la descarga local sin estado nuevo en el contrato. El backend actual cubre el envío de lotes y el seguimiento del run (`POST` y `GET /api/v1/runs/{run_id}`), pero no las lecturas ni las revisiones.

**Decisiones cerradas**

- Procesamiento, aprobación y almacenamiento se mantienen como tres estados separados; guardar en OCI no implica publicar.
- Toda decisión del curador (editar, aprobar, rechazar) crea una revisión nueva. Las anteriores no se modifican, porque el objeto de OCI es inmutable.
- La interfaz envía la revisión base; si otra sesión ya creó una posterior, el backend responde conflicto y no sobrescribe.
- La copia local es una descarga desde la UI; no se registra evidencia local ni se agrega `guardado_local`.

**Hallazgos del código que cambian el diseño**

1. **`texto_limpio` no es texto limpio.** La columna guarda el texto original ya validado: el validador no lo modifica y el enmascarado de datos personales ocurre solo dentro del analizador, antes de llamar al LLM. Por eso la interfaz nombra el campo `texto` con `tipo_texto = "original_validado"`, como pidió Sergio, y la política de datos personales queda como decisión pendiente (sección 7).
2. **Una ejecución recién encolada no existe en `lotes_procesamiento`.** Solo está en `solicitudes_ingesta`, que no guarda comunidad ni período. El histórico parte del registro de la API y completa esos campos cuando el lote ya se procesó.
3. **`registrar_paquete` sobrescribe la revisión que recibe.** Es correcto para reejecutar el pipeline, pero para curaduría hace falta una vía de solo inserción. Comprobado: dos sesiones que guardan la revisión 3 dejan solo la última, sin error.
4. **Las funciones de invariantes viven en `pipeline_runner.py`**, que importa `ai_modules.generador` y LangGraph. La API no debe depender de eso para validar una edición; hay que moverlas a un módulo propio (`orchestration/invariantes.py`) y reexportarlas.
5. **`leer_rechazados` solo devuelve rechazos del validador**; los de origen `analisis` están en la tabla pero sin consulta.

## 2. Interfaz común

Cada operación existe como función de Python (`storage/consultas.py` y `storage/revisiones.py`) y como ruta HTTP bajo `/api/v1`, con los mismos nombres, parámetros y respuestas. Las rutas son un envoltorio fino en `api/rutas_curaduria.py` y usan la misma autenticación por `X-Webhook-Secret`. Las respuestas son modelos Pydantic; las listas llevan siempre un sobre con el estado de la ejecución (sección 3). Todas las rutas heredan `401 no_autorizado`.

| # | Operación (ruta HTTP) | Parámetros | Respuesta | Errores propios |
| --- | --- | --- | --- | --- |
| 1 | `listar_ejecuciones` (`GET /runs`) | `limite` 1–100 (20), `cursor`, `estado`, `periodo`, `comunidad` | Histórico: `items` con `run_id`, `request_id`, `fecha_recepcion`, `fecha_procesamiento`, `comunidad`, `periodo`, `estado`, `categoria_estado`, `tiene_paquete`; `siguiente_cursor`; `total` | `parametro_invalido` 422 |
| 2 | `obtener_ejecucion` (`GET /runs/{run_id}/detalle`) | `run_id` | Identidad, `estado`, `categoria_estado`, `total_recibidos`, `total_validos`, `total_rechazados`, `estado_flujo`, `tiene_paquete`, `revision_vigente`. Funciona sin paquete | `ejecucion_no_encontrada` 404 |
| 3 | `listar_mensajes` (`GET /runs/{run_id}/mensajes`) | `apto`, `categoria`, `sentimiento`, `requiere_soporte`, `limite` 1–200 (50), `cursor` | Por mensaje: `source_id`, `categoria_enrutamiento`, `sentimiento`, `puntuacion` (tres criterios y `total`), `motivo_seleccion`, `regla_aplicada`, `temas`, `entidades`, `requiere_soporte`, `apto_para_publicacion` | 404, 422 |
| 4 | `obtener_fuentes` (`GET /runs/{run_id}/fuentes?source_ids=a,b`) | `source_ids` 1–50 | Por fuente: `source_id`, `autor`, `canal`, `tipo`, `texto`, `tipo_texto = "original_validado"`; los ids que no existen van en `no_encontrados` | 404 de la ejecución; 422 |
| 5 | `obtener_resumen` (`GET /runs/{run_id}/resumen`) | `run_id` | `total_interacciones_procesadas`, `registros_validos`, `registros_rechazados`, `sentimiento_predominante`, `distribucion_sentimiento`, `temas_principales`, `alertas_soporte` | 404 |
| 6 | `listar_rechazados` (`GET /runs/{run_id}/rechazados`) | `origen` (`validador` o `analisis`, por defecto ambos), `incluir_texto` (falso) | Por rechazo: `mensaje_id`, `autor`, `canal`, `motivo`, `origen`, `fecha_aislamiento`; `texto_crudo` solo si se pide. Reutiliza `leer_rechazados` | 404, 422 |
| 7 | `listar_revisiones` (`GET /runs/{run_id}/revisiones`) | `run_id` | Orden descendente: `numero_revision`, `estado`, `revisor`, `fecha_decision`, `fecha_generacion`, `status_almacenamiento`, `comprobacion_lectura`; `revision_vigente` | 404 |
| 8 | `obtener_revision` (`GET /runs/{run_id}/revisiones/{n}`) | `numero_revision` | `paquete` (Contrato 3 completo, de `reconstruir_json_paquete_salida`) y `evidencia_oci` | `revision_no_encontrada` 404 |
| 9 | `registrar_revision` (`POST /runs/{run_id}/revisiones`) | `revision_base`; `edicion`: `estado` (pendiente, aprobado, rechazado), `revisor`, `comentarios`, `post_linkedin` y `resumen_semanal` opcionales | `201` con `numero_revision`, `estado`, `ruta_objeto` | `conflicto_revision` 409 con `revision_actual`; `revision_invalida` 422; 404 |
| 10 | `guardar_revision_en_oci` (`POST /runs/{run_id}/revisiones/{n}/oci`) | `numero_revision` | `200` con `bucket`, `ruta_objeto`, `status_almacenamiento`, `comprobacion_lectura`, `mensaje_error`, `reintento_idempotente` | 404 |

Todos los errores HTTP usan el formato actual de la API: `{"error": "<codigo>", "detalle": "..."}`. Cualquier fallo de SQLite en una lectura devuelve `lectura_fallida` 503; nunca una lista vacía.

**Diferencia con lo propuesto por Sergio:** la consulta de fuentes acepta una lista de ids (1 a 50) para cargar de una vez las fuentes de un borrador; con un solo id equivale a la consulta por `run_id` y `source_id`. Para ver las fuentes de un borrador no hace falta otra ruta: `obtener_revision` ya incluye `source_ids` en cada activo.

## 3. Estados y errores: qué recibe la interfaz en cada caso

La interfaz distingue cuatro situaciones sin interpretar listas vacías. `categoria_estado` se deriva del `estado` del registro de la API:

| `estado` del registro | `categoria_estado` | ¿Hay paquete? |
| --- | --- | --- |
| `recibiendo`, `en_cola`, `procesando` | `en_curso` | No todavía |
| `completado` | `finalizada_con_paquete` | Sí (se comprueba en `paquetes_salida`) |
| `sin_registros_validos`, `sin_contenido_publicable` | `finalizada_sin_paquete` | No |
| `fallido` | `fallida` | No |

| Situación | Cómo se reconoce | Respuesta |
| --- | --- | --- |
| Ejecución inexistente | El `run_id` no está en `solicitudes_ingesta` ni en `lotes_procesamiento` | `404 ejecucion_no_encontrada` en cualquier operación |
| Todavía procesando | `categoria_estado = en_curso` | `200` con `disponible: false` y `motivo: "en_curso"`; la lista va vacía pero la interfaz sabe por qué. `obtener_ejecucion` devuelve el estado y los totales que ya existan |
| Finalizada sin paquete | `sin_contenido_publicable`: hay análisis y no hay paquete. `sin_registros_validos`: solo hay rechazos | Mensajes, resumen y rechazados con `disponible: true` cuando hay filas; revisiones con `disponible: false` y `motivo: "sin_paquete"`. Sin análisis, `motivo: "sin_analisis"` |
| Fallida | `categoria_estado = fallida` | Lo que haya quedado persistido, con la categoría en el sobre para que la UI no lo presente como éxito (una caída total del LLM deja mensajes degradados y un resumen con `registros_validos = 1`) |
| Fallo de lectura | Excepción de SQLite | `503 lectura_fallida`; nunca una lista vacía |

Los sobres de lista comparten `run_id`, `estado`, `categoria_estado`, `disponible` y `motivo`. Si un run figura `completado` pero no tiene paquete, `obtener_ejecucion` lo marca con `tiene_paquete: false` y una advertencia en lugar de ocultarlo.

**Histórico.** Se ordena por fecha de recepción descendente y `run_id`, con cursor opaco (fecha y `run_id` codificados). A diferencia de un desplazamiento numérico, no repite ni salta filas cuando llegan lotes nuevos durante la paginación. Une el registro de la API con los lotes procesados que no pasaron por la API (ejecuciones manuales), que aparecen con `fecha_recepcion` nula. Mientras un lote está en cola, `comunidad` y `periodo` van nulos porque el registro aún no los guarda.

## 4. Revisiones y guardado en OCI

Cada decisión del curador crea la revisión `n+1` y se guarda en `activos/{periodo}/{run_id}/revision-{NNN}.json`. La ruta incluye el período; el criterio de aceptación de la propuesta original lo omitía. Aprobar sin cambios también crea una revisión, porque la revisión 1 ya está en OCI y es inmutable. Editar un contenido aprobado crea una revisión `pendiente` que exige nueva aprobación; la aprobada queda intacta.

**`registrar_revision(run_id, revision_base, edicion)`**

1. Lee la revisión base con `reconstruir_json_paquete_salida`. Si no existe, responde `404 revision_no_encontrada`.
2. Valida la edición antes de tocar la base: `revisor` obligatorio si el estado es `aprobado` o `rechazado`; los `source_ids` del post deben ser mensajes aptos y los del resumen, mensajes procesados; los textos cumplen los mínimos del contrato. Si algo falla, `422 revision_invalida` con la lista de problemas.
3. Abre una transacción `BEGIN IMMEDIATE` y lee la revisión más alta. Si no coincide con `revision_base`, responde `409 conflicto_revision` con `revision_actual` y no escribe nada.
4. Inserta la revisión nueva solo con `INSERT`, sin el `DELETE` previo de `registrar_paquete`. Copia de la base el análisis resumido, los casos no publicables y los metadatos de ejecución (describen la generación, no la edición), aplica el contenido editado, registra revisor y `fecha_decision` en UTC cuando no es `pendiente`, y deja la evidencia OCI en `no_iniciado` con la ruta calculada por `build_object_path`.
5. Si la clave `(run_id, numero_revision)` ya existiera, el error de integridad se traduce al mismo conflicto. Devuelve `numero_revision` y `ruta_objeto`.

**`guardar_revision_en_oci(run_id, numero_revision)`**

1. Si la evidencia ya es `guardado_con_exito` con lectura verificada, la devuelve sin subir de nuevo.
2. Reconstruye el paquete desde SQLite con la evidencia en `no_iniciado`. Son los mismos bytes en cada intento, así un reintento tras un `412` se resuelve como idempotente en el cliente OCI.
3. Llama a `OCIStorageClient.upload_package(numero_revision=n)` en un hilo. El método no lanza excepciones: devuelve un `UploadResult`.
4. Registra el resultado real con un método nuevo, `actualizar_evidencia_oci(run_id, n, status, comprobacion_lectura)`. La UI nunca fija `comprobacion_lectura`.
5. Un fallo de OCI (incluida la falta de credenciales) responde `200` con `guardado_error` y el mensaje; la revisión sigue en SQLite y el mismo endpoint se puede reintentar.

**Cambios en código existente**

- `_insertar_paquete` recibe `sobrescribir` (verdadero por defecto, para no alterar la reejecución del pipeline).
- `storage/local_db.py` gana `actualizar_evidencia_oci`; hoy esa actualización solo existe como un `UPDATE` interno de `sincronizar_pendientes_oci`, que pasará a usarlo.
- `calcular_enfoque_editorial`, `invariante_casos` y `violaciones_trazabilidad` pasan a `orchestration/invariantes.py` y `pipeline_runner.py` las reexporta, para que la API no importe el generador.

## 5. Fases, responsables y cronograma

Quedan tres días hábiles hasta el Demo Day: sábado 24, domingo 25 y lunes 26 de octubre, más la mañana del martes 27 para el ensayo. Los responsables son una propuesta, a confirmar con George. El frontend no espera al backend: desde la Fase 0 trabaja contra ejemplos JSON de cada respuesta.

| Fase | Entregable | Responsable propuesto | Plazo | Criterio de salida |
| --- | --- | --- | --- | --- |
| 0. Congelar la interfaz | Este documento revisado, decisiones D1 a D3 de la sección 7 y un ejemplo JSON por operación y por situación de la sección 3 | Michael con Sergio | Sáb 24, mañana | Sergio confirma nombres, parámetros y errores; desde aquí no cambian sin acuerdo |
| 1. Preparación del código | `orchestration/invariantes.py` con reexportación; `sobrescribir` en `_insertar_paquete`; `actualizar_evidencia_oci` | Michael; Enrique revisa el cambio en `pipeline_runner.py` | Sáb 24 | La suite actual sigue en verde sin cambios de comportamiento |
| 2. Lecturas | `storage/consultas.py` con las operaciones 1 a 8 y sus modelos de respuesta | Michael; Andrea revisa consultas e índices | Sáb 24 a dom 25 (mañana) | Pruebas de los cuatro estados de la sección 3, de paginación y de fallo de lectura |
| 3. Revisiones y OCI | `storage/revisiones.py` con las operaciones 9 y 10 | Michael | Dom 25 | Prueba de dos hilos con la misma revisión base: uno gana y el otro recibe conflicto; reintento idempotente en OCI |
| 4. Rutas HTTP | `api/rutas_curaduria.py`, errores en el formato actual, autenticación y pruebas con `TestClient` | Enrique con Michael | Dom 25 (tarde) a lun 26 (mañana) | Cada operación responde igual por HTTP y por Python; ninguna ruta funciona sin el secreto |
| 5. Integración | Pantallas conectadas; recorrido completo con evidencia (issues #10 y #11) sobre el generador ya integrado en `main` | Sergio y QA | Lun 26 | Un lote real recorre API, worker, SQLite, revisión humana y OCI |
| 6. Cierre | Congelamiento del código el lunes por la noche; ensayo el martes por la mañana | George | Lun 26 noche, mar 27 | Solo correcciones de errores bloqueantes |

**Frontend en paralelo (Sergio).** Cliente `httpx` con tiempos límite y manejo de 401, 404, 409, 413, 422 y 503; seguimiento del run con actualizaciones acotadas y `run_id` conservado ante cortes de red; las tres vistas de estado por separado.

**Línea de recorte.** Si el domingo por la noche las Fases 2 y 3 no están cerradas, se recorta en este orden: primero los filtros del histórico; luego el cursor (se usa `limite` y desplazamiento); luego las rutas HTTP de lectura, que Streamlit puede reemplazar importando `storage/consultas.py` mientras corra en el mismo equipo que el worker. No se recorta nada de `registrar_revision` ni del guardado en OCI con evidencia real, porque son lo que evita perder ediciones y declarar éxito sin verificar la nube.

## 6. Pruebas y criterios de aceptación

Las pruebas del backend usan SQLite real en un directorio temporal; solo se simulan el LLM y el backend de OCI, como en `tests/test_pipeline_telemetria.py`. Los criterios se verifican con el recorrido de la API al worker, el orquestador y la base compartida.

| Área | Qué debe demostrarse |
| --- | --- |
| Estados | Los cuatro casos de la sección 3 devuelven la respuesta indicada: `404` para un run inexistente, `disponible: false` con `en_curso`, datos sin paquete para `sin_contenido_publicable` y `sin_registros_validos`, y la categoría `fallida` tras una caída total del LLM |
| Fallo de lectura | Con la base bloqueada o dañada, cada operación devuelve `503 lectura_fallida` y nunca una lista vacía |
| Histórico | Con 120 ejecuciones, las páginas no repiten ni omiten filas aunque entren lotes nuevos entre llamadas; los filtros por estado, período y comunidad coinciden con SQL directo; aparecen las ejecuciones manuales |
| Concordancia | Los totales del detalle, las filas de mensajes y los rechazos coinciden con `lotes_procesamiento`, `mensajes_analizados` y `auditoria_rechazados` del mismo run |
| Fuentes | `texto` coincide con el texto original ingresado, `tipo_texto` es `original_validado` y los ids inexistentes salen en `no_encontrados` |
| Revisiones | La revisión n+1 conserva la n intacta; aprobar sin cambios crea una revisión; editar una aprobada crea una `pendiente`; los `source_ids` fuera de lo permitido devuelven `422` |
| Concurrencia | Dos hilos con la misma `revision_base`: uno recibe `201` y el otro `409` con `revision_actual`; no se pierde ninguna edición |
| OCI | La subida deja la evidencia con el resultado real; un segundo llamado no sube de nuevo; con el objeto ya existente y el mismo contenido, se resuelve como idempotente; sin credenciales queda `guardado_error` y se puede reintentar |
| Seguridad | Sin `X-Webhook-Secret` o con uno inválido, todas las rutas responden `401`; el secreto no aparece en logs ni respuestas |
| Regresión | La suite actual (255 pruebas) sigue en verde y `ruff` queda limpio en los archivos nuevos |

**Prueba de integración final (lunes 26).** Con el generador real ya en `main` (PR #24 fusionado) y credenciales de OCI reales, un lote recorre envío, procesamiento, revisión humana, nueva revisión y guardado en OCI verificado. Se conservan las evidencias vinculadas al commit probado. Las pruebas del backend de este plan simulan el LLM; la primera ejecución con el módulo real del generador sigue pendiente (ver riesgos).

## 7. Riesgos y decisiones pendientes

**Decisiones a cerrar el sábado 24 (Fase 0)**

| # | Decisión | Recomendación | Con quién |
| --- | --- | --- | --- |
| D1 | Datos personales en `texto`: el texto guardado es el original y no está enmascarado | Para el MVP, mostrarlo tal cual, etiquetado `original_validado`, porque el curador necesita verificar la fuente; el panel queda detrás del secreto. Los rechazos no incluyen texto salvo `incluir_texto=true`. Alternativa: promover `_enmascarar_pii` del analizador a utilidad pública y aplicarla en la lectura | Michael y Sergio |
| D2 | Interfaz única: HTTP como contrato y Python como el mismo código por debajo | Confirmar HTTP para la UI (Sergio ya lo asume) y dejar la importación directa solo como recorte | Sergio |
| D3 | Aprobar sin editar crea una revisión nueva | Confirmar, porque la revisión 1 ya está en OCI y es inmutable | Sergio |
| D4 | Responsables y plazos de la sección 5 | Confirmar | George |

**Riesgos**

| Riesgo | Efecto | Mitigación |
| --- | --- | --- |
| El generador real no se ha probado con el orquestador ni con la telemetría de este plan | Una diferencia de firma en `generar_activos_marketing` o `TelemetriaGeneracion` rompe el flujo o la telemetría. Mis pruebas usan un sustituto escrito a partir de cómo lo invoca `pipeline_runner.py`, no el módulo real | Primera tarea de la Fase 1: actualizar con `main`, correr la suite con el módulo real y ajustar o eliminar el sustituto de `tests/test_pipeline_telemetria.py` |
| Tres días hábiles | Recortes de la sección 5 | La línea de recorte protege revisiones y OCI, que son lo no negociable |
| Lecturas de la UI mientras el worker escribe | Lecturas bloqueadas o inconsistentes | La base ya usa WAL; las conexiones de lectura llevan `PRAGMA query_only` y tiempo de espera. Solo lo probé de forma secuencial, no con carga concurrente |
| Textos de mensajes expuestos por HTTP | Fuga de datos personales si el puerto queda público | Secreto obligatorio y `/docs` apagado por defecto; el informe de infraestructura advierte que el puerto 8000 puede quedar expuesto, así que hay que restringirlo |
| Reejecutar un run cuya revisión 1 ya se subió a OCI | `guardado_error` por colisión de contenido (ver spec del runner v3.2, sección 7.2) | Este plan no lo agrava: las revisiones 2 en adelante se suben desde SQLite con bytes deterministas. Sigue pendiente para la revisión 1 |
| No existe el estado "publicado" en el contrato | La UI podría sugerir publicación al guardar en OCI | Mostrar "aprobado" y "guardado en OCI" como estados separados; "publicado" queda fuera del MVP |
