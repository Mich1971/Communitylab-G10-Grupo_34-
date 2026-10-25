# Panel de curaduría · integración HTTP

El panel conserva las cuatro pantallas y consume la API de curaduría. No abre
SQLite, no genera contenido y no llama directamente a OCI. Requiere el backend
de `feat/consultas-curaduria-revisiones` (base probada: `3adc54c`).

## Ejecución

Desde la raíz, con Python 3.11 y uv:

```powershell
uv sync --frozen
```

Configurar en `.env` (sin versionarlo):

```dotenv
COMMUNITYLAB_API_URL=http://127.0.0.1:8000
COMMUNITYLAB_WEBHOOK_SECRET=<mismo secreto del backend, al menos 16 caracteres>
COMMUNITYLAB_DATA_DIR=data
```

Iniciar cada proceso en una terminal desde la raíz:

```powershell
uv run --frozen uvicorn api.main:app --host 127.0.0.1 --port 8000
uv run --frozen python -m orchestration.worker
uv run --frozen streamlit run ui/app.py --server.address 127.0.0.1
```

El worker requiere la configuración de IA y OCI descrita por backend. El panel
solo requiere URL y secreto. No utiliza datos simulados como sustituto cuando
la API falla. El ejemplo de Carga se envía al procesamiento real: puede consumir
el proveedor configurado, igual que cualquier lote subido.

## Pantallas

1. **Carga:** enviar JSON, reutilizar una ejecución duplicada y abrir histórico
   paginado. Los identificadores vienen de la API.
2. **Resultados:** métricas, seguimiento acotado a dos minutos, resumen,
   mensajes paginados por categoría, fuentes y rechazos de ambos orígenes.
3. **Edición y aprobación:** cargar una revisión, editar ambos activos,
   consultar fuentes y guardar una revisión pendiente, aprobada o rechazada.
   Las fuentes, hashtags y demás campos no editables se conservan. El backend
   asigna el número de revisión. El rechazo exige motivo en la interfaz.
4. **Almacenamiento:** consultar cualquier revisión, descargarla y solicitar
   guardado/reintento en OCI. Solo se muestra éxito si el backend confirma estado
   exitoso y lectura verificada. No implica publicación en LinkedIn.

## Recuperación y límites

- `run_id` en la URL permite recuperar la selección al recargar la página.
- Ediciones no guardadas sobreviven la navegación interna, no el cierre de la
  sesión. Descargar el borrador antes de cambiar de lote o cargar otra revisión.
- Ante 409 se conserva el texto y la revisión base, se ofrece comparación con
  la vigente y se bloquea una nueva escritura hasta cargar explícitamente una
  revisión. Descargar/copiar la edición antes de hacerlo.
- Un timeout de escritura puede ocurrir después del guardado. No se reintenta
  automáticamente: consultar las revisiones y reconciliar antes de repetir.
- El secreto autentica al servidor ante la API; no autentica al visitante del
  panel. Restringir el acceso al despliegue y usar datos sintéticos en demos.
- No se muestran detalles técnicos arbitrarios del backend en mensajes de red.
- HTTPX pasa de dependencia de desarrollo a ejecución; el lockfile conserva
  las versiones, sin incorporar otro cliente HTTP.

## Pruebas

```powershell
uv run --frozen pytest -q
uv run --frozen pytest tests/test_ui_api_client.py tests/test_ui_http_flow.py tests/test_frontend.py -q
```

Las pruebas de UI utilizan FastAPI y SQLite temporales. Las fuentes y borradores
son fixtures sintéticos; OCI se sustituye para probar errores sin nube. Consultar
[la evidencia de integración](../docs/evidencias/2026-10-25-integracion-http.md).
La [documentación anterior](../docs/panel-sergio.md) describe la demo histórica,
no el comportamiento HTTP actual.
