# Evidencia · integración HTTP del panel

Fecha: 25 de octubre de 2026. Base backend: `3adc54c`, rama
`feat/consultas-curaduria-revisiones`. Aporte: `feat/ui-integracion-http`.

## Resultado

El panel usa las diez rutas de curaduría y las rutas de admisión/seguimiento.
Se conserva el diseño y se reemplaza el flujo simulado por operaciones HTTP.
El backend administra la base y la numeración de revisiones; la UI preserva las
fuentes y no asigna evidencia OCI.

## Verificación realizada

- `uv run --frozen pytest -q`: **374 passed, 1 skipped, 2 deselected**.
  La omisión corresponde a una prueba del worker que no aplica cuando existe
  el runner. Las pruebas marcadas de integración real quedaron excluidas.
- Tras el último ajuste de manejo de respuesta: pruebas focalizadas del panel,
  cliente y respaldo histórico: **16 passed**.
- `ruff check` de los cinco archivos Python modificados/nuevos: sin hallazgos.
- AppTest contra FastAPI y SQLite temporal: lectura, aprobación, conflicto por
  revisión concurrente, conservación del texto al navegar, estados sin paquete
  y respuesta HTTP 200 con `guardado_error` sin mostrar éxito.
- Cliente HTTP: autenticación, códigos 401/404/409/413/422/503, JSON inválido,
  timeout de escritura sin reintento automático y validación de modelos de respuesta.
- Navegador: ejecución sintética recuperada por API, editor y aprobación de
  revisión 1 a 2; consulta posterior mostró `aprobado` y OCI `no_iniciado`.

## Capturas reales de la vista previa

Las imágenes contienen datos sintéticos sembrados en SQLite temporal. No son
prueba de generación LLM ni subida a OCI. Se abrió Streamlit contra una API
local; la aprobación sí persistió en la base temporal.

![Editor conectado](ui-http/03-editor.png)

![Aprobación y almacenamiento separados](ui-http/04-almacenamiento.png)

## Pendiente para aceptación E2E

Ejecutar API, worker, proveedores de IA y OCI con la configuración del equipo;
subir un lote y verificar la nueva revisión recuperada de OCI. Esta entrega no
acredita ese recorrido externo ni publica contenido en LinkedIn.

La rama depende del backend de curaduría (PR #28). Mientras ese PR no esté en
`main`, el diff de este PR contra `main` incluye también los commits del backend:
para ver solo el aporte del panel, comparar contra `feat/consultas-curaduria-revisiones`
(`git diff origin/feat/consultas-curaduria-revisiones...feat/ui-integracion-http`).

## Actualización · 25 de octubre (tarde): verificación, revisión del backend y mejoras

Qué se hizo después de abrir este PR, para que el equipo pueda repetirlo y entender
cada decisión.

### 1. Verificación reproducible

```powershell
uv sync --frozen
uv run --frozen pytest -q
```

Si `pytest` falla al recolectar con `ModuleNotFoundError: structlog` (u otra
dependencia), el `.venv` está desactualizado: `uv sync --frozen` lo corrige.
Resultado en este PR: 374 passed, 1 skipped, 2 deselected.

Recorrido manual de las cuatro pantallas contra la **API real** (uvicorn) y
Streamlit, con una base temporal y un lote sintético:

| Pantalla | Qué se comprobó |
|---|---|
| Carga | El histórico llega de la API y `run_id` queda en la URL |
| Resultados | Métricas, categorías, fuentes y rechazos desde el backend |
| Edición y aprobación | Cargar revisión 1, aprobar como revisor → la API muestra la revisión 2 `aprobado`, con un solo `POST` |
| Almacenamiento | OCI `no_iniciado` y aviso "todavía no tiene almacenamiento OCI verificado" (no inventa éxito) |

Límite: el recorrido usó datos sintéticos sembrados. No pasó por el worker, por un
LLM ni por OCI reales (ver "Pendiente").

### 2. Mejora en el panel: `reintento_idempotente`

El backend ya devuelve `reintento_idempotente` al guardar una revisión en OCI, pero
la pantalla 4 no lo mostraba. Ahora, si el guardado se confirma con lectura
verificada y la respuesta indica reintento idempotente, el panel avisa de que el
objeto ya existía con el mismo contenido y no se duplicó (`ui/app.py`).

Decisión: **no** se cambió `esquemas.py`. `EvidenciaAlmacenamientoOCI` no incluye
ese campo y cambiar el contrato requiere consenso del equipo; el indicador solo viaja
en la respuesta del guardado y se recuerda en la sesión durante el `rerun`.
Prueba: `test_oci_success_shows_idempotent_retry_notice` en
`tests/test_ui_http_flow.py` (con y sin reintento idempotente).

### 3. Revisión de código del PR #28 (backend)

Se revisó el #28 buscando fallos que las pruebas no cubren. Nueve hallazgos; el
detalle y los escenarios están en el comentario del PR #28. Resumen:

- **Corregido** (PR #30): al arrancar limpio, la API respondía 503 en las consultas
  porque las tablas del esquema local solo las creaba el worker. Reproducido con
  `GET /api/v1/runs` sobre un directorio de datos vacío.
- **Pendientes, menores** (no bloquean la fusión): guarda al actualizar la evidencia
  OCI ante guardados simultáneos; validación de tipos del cursor (503 en vez de 422);
  el backend acepta rechazar sin motivo; sin límite de tamaño en el POST de
  revisiones; `JOIN` interno en el listado de revisiones; listado de ejecuciones
  recorre la tabla completa por página; ruta de objeto OCI duplicada; comentario
  de `CASCADE` que contradice los `DELETE` explícitos.

### 4. Orden de fusión recomendado

1. PR #30 → rama de #28 (arreglo del 503; lo revisa quien mantiene el backend).
2. PR #28 → `main`.
3. PR #29 → `main` (este panel). Su diff se reduce solo al aporte del frontend.

## Pendiente para aceptación E2E (sin cambios)

Falta el recorrido con worker, IA y OCI reales. Requiere en `.env` la clave del
proveedor de IA (por defecto `GEMINI_API_KEY`) y las variables `OCI_*` del equipo
(las gestiona Michael); no deben versionarse. Con eso: levantar API, worker y panel
como indica `ui/README.md`, subir un lote desde Carga y verificar el estado OCI con
lectura verificada. Sin esas credenciales, el PR es válido pero este tramo queda
declarado como no probado.

## Recorrido real con Gemini y OCI (25 de octubre, noche)

Se ejecutaron API, worker y panel con un directorio de datos limpio, la clave de
Gemini de Sergio y un **bucket de prueba personal** en OCI (`sa-santiago-1`, privado,
creado solo para esta prueba). **No es el bucket del equipo (`mx-queretaro-1`).**
Lote: `data/raw/lote_prueba_01.json` con otro `request_id`; el `request_id` fijo del
botón "Enviar datos de ejemplo" se trata como duplicado tras el primer envío.

### Qué se comprobó de verdad

| Tramo | Resultado |
|---|---|
| API → worker → pipeline | Run `completado`: 4 recibidos, 3 válidos, 1 rechazado |
| Análisis cognitivo | **Real** (Gemini): sentimiento "Mixto", temas y categorías (Logro/Duda) |
| Guardado en OCI desde el worker | `put_object` + lectura de verificación, `guardado_con_exito`, `comprobacion_lectura=true` |
| Aprobación en el panel | Revisión 2 `aprobado` por el revisor |
| Guardado en OCI desde el panel | Revisión 2 → "Guardado en OCI y lectura verificada por el backend" |
| Verificación independiente | Los objetos `activos/2026-W38/<run>/revision-001.json` y `revision-002.json` existen en el bucket |

Capturas (datos sintéticos, sin claves ni credenciales visibles):

![Carga: run completado](evidencias/e2e-real/01-carga-run-completado.jpg)
![Resultados con análisis de Gemini](evidencias/e2e-real/02-resultados-analisis-gemini.jpg)
![Editor con el texto de la plantilla de respaldo](evidencias/e2e-real/03-editor-plantilla-respaldo.jpg)
![Almacenamiento: aprobado, OCI sin verificar](evidencias/e2e-real/04a-almacenamiento-sin-verificar.jpg)
![Almacenamiento: verificado en OCI](evidencias/e2e-real/04b-almacenamiento-verificado-oci.jpg)

### Qué NO quedó probado

**La generación de contenido con IA no llegó a funcionar.** El texto del editor
("Comunidad: OCI …") es la **plantilla de respaldo** (`generacion_ia_degradada_a_plantilla`),
no salida del modelo. Causa: la clave usada es de capa gratuita y Google responde
`429 You exceeded your current quota` tras ~6 llamadas por minuto (medido con una
ráfaga de 8 llamadas); además `gemini-3.8-flash` respondió `503` por alta demanda y
`gemini-flash-latest` dio timeouts. Subir `LLM_MAX_RETRIES` a 8 no bastó. Un run
posterior terminó `fallido` por esos errores. Es un límite de cuota, no del panel ni
del backend, pero **debe repetirse con una clave con cuota suficiente** antes de dar
por validada la generación.

### Hallazgo para el equipo: modelos Gemini retirados

- El valor por defecto del código (`ai_modules/multiprovider_agent.py`) es
  `gemini-2.5-flash` y `.env.example` documenta `gemini-1.5-flash`. Con una clave
  nueva, `gemini-2.5-flash` responde `404 … no longer available to new users`.
- Solución local usada: `GEMINI_MODEL` en el `.env` (probado: `gemini-3.5-flash` y
  `gemini-flash-latest` responden). Conviene actualizar `.env.example` y el valor por
  defecto; no se hizo aquí porque es código de backend (#28).

### Cómo repetirlo

1. `GEMINI_API_KEY` y `GEMINI_MODEL` en `.env`; bucket privado propio y `OCI_REGION`,
   `OCI_BUCKET_NAME`, `OCI_CONFIG_PROFILE` (comentar `OCI_USER_OCID`, `OCI_TENANCY_OCID`,
   `OCI_FINGERPRINT` y `OCI_KEY_FILE` para usar `~/.oci/config`).
2. `uv run --frozen uvicorn api.main:app`, `uv run --frozen python -m orchestration.worker`
   y `uv run --frozen streamlit run ui/app.py` (con `COMMUNITYLAB_WEBHOOK_SECRET`).
3. Esperar al menos un minuto entre lotes si la clave es de capa gratuita.
