# Integración HTTP del panel · 25 de octubre de 2026

Base: `feat/consultas-curaduria-revisiones`, commit `3adc54c`.
Diseño: conservar Streamlit, logo, estilos y cuatro pantallas. La API es la
única vía de lectura y escritura; la UI no abre SQLite ni llama al SDK OCI.
Referencia: `docs/plan_implementacion_consultas_UI.md` y modelos implementados
en `storage/modelos_curaduria.py` (prevalecen sobre los ejemplos del catálogo).

## Implementación y validación

- [x] Cliente HTTP (`ui/api_client.py`) con configuración centralizada,
  tiempos límite, autenticación servidor a servidor y errores seguros.
  Probar errores HTTP, respuesta inválida y timeout sin reintentar escrituras.
- [x] Cuatro pantallas (`ui/app.py`): carga/histórico paginado, seguimiento
  acotado, análisis/fuentes, edición de revisión y evidencia OCI.
- [x] Conservar el borrador en cambios de pantalla, fallos y conflictos.
  Reservar revisiones solo al guardar/decidir. Mantener fuentes inmutables.
  Una respuesta de escritura perdida obliga a revisar el histórico antes de
  repetir; no se deduce éxito ni se cambia automáticamente la revisión base.
- [x] AppTest contra FastAPI y SQLite temporal: navegación, aprobación,
  conflicto, fuentes y guardado fallido. Mantener pruebas del respaldo local.
- [x] Ejecutar suite, documentar resultados y preparar entrega desde el fork.

No se cambia el contrato ni el backend. No hay fallback automático a mocks.
La selección persistida se recupera mediante `run_id` en la URL; las ediciones
no guardadas permanecen solo en la sesión y se pueden descargar.

