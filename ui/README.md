# CommunityLab: panel de curaduría

**Entrega actualizada del módulo (Sergio, 14/10/2026):** consultar
[diseño, implementación, ejecución, conexiones, pruebas y pendientes](../docs/panel-sergio.md).
Ese documento distingue `main`, los PR #17/#18 pendientes y la nueva rama SQLite.
Las verificaciones fechadas más abajo se conservan como antecedentes históricos.

Diseño aprobado: variante clara con acento verde petróleo, navegación en cuatro pasos y mensajes originales junto al análisis. Referencia visual: [`docs/screenshots/00-mockup-aprobado.png`](../docs/screenshots/00-mockup-aprobado.png). Logo oficial integrado en la barra lateral desde `ui/assets/communitylab-logo.png`.

Capturas de las 4 pantallas (carga, resultados, edición/aprobación con el camino de rechazo, almacenamiento) en [`docs/screenshots/`](../docs/screenshots/) y en el [README principal](../README.md) (sección "Panel de curaduría").

## Ejecutar

Desde la raíz del repositorio, con las dependencias instaladas:

```powershell
python -m streamlit run ui/app.py
python -m pytest tests/ -q
```

La sesión conserva el progreso al navegar y al recargar el mismo lote mediante su huella de contenido. Un lote diferente reemplaza el trabajo de la sesión; descargarlo previamente. El estado no persiste al cerrar la sesión.

## Archivos

- `app.py`: presentación y navegación Streamlit; consume los contratos y el validador existentes.
- `styles.css`: colores, espaciado y estilos de presentación separados del flujo.
- `mock_pipeline.py`: análisis y generación heurísticos preexistentes; no es IA real.
- `storage_demo.py`: escritura exclusivamente local con lectura posterior, reintentos idénticos y rechazo de colisiones. No se conecta automáticamente a OCI.
- `tests/test_frontend.py`: flujo de curaduría, conteos, aprobación, nueva revisión tras edición, conservación del mismo lote e inmutabilidad local.

## Integración pendiente

Conectar análisis/generación reales y un adaptador OCI explícito. El paquete mantiene `almacenamiento_oci.status_almacenamiento = no_iniciado` aunque la copia local esté verificada: la evidencia local no demuestra almacenamiento en la nube. La UI señala esta diferencia y no solicita credenciales.

La descarga de borradores incluye los textos actuales aunque todavía no se hayan guardado en sesión. Aprobar exige validación de ambos formatos y nombre del revisor; rechazar exige además un motivo. Editar contenido aprobado elimina su aprobación y aumenta la revisión.

## Verificación

**30 de septiembre de 2026:** las 4 pruebas pasan con el `.venv` del proyecto (Python 3.11.16, `uv venv --python 3.11`) usando las versiones exactas fijadas en `requirements.txt` (Streamlit 1.38.0, Pydantic 2.8.2). Recorrido manual de las 4 pantallas en navegador con el lote de ejemplo: carga (4/3/1), análisis, edición con validación de campos obligatorios, aprobación, guardado local verificado. Se comparó la pantalla de Resultados contra el mockup aprobado (`docs/screenshots/00-mockup-aprobado.png`): coincide en layout, badges y estadísticas; solo difiere el texto de justificación porque proviene de `mock_pipeline.py` (heurística local), no de un LLM real. Probado también en ancho móvil (375px): las columnas se apilan correctamente.

*Nota histórica (29 de septiembre de 2026):* una verificación anterior reportó el `.venv` como roto y usó Python 3.12.3 de Anaconda en su lugar. Eso era incorrecto o quedó desactualizado — el `.venv` del proyecto funciona correctamente y es el que debe usarse.
