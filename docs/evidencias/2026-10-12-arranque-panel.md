# Corrección del arranque del panel de curaduría

Fecha: 12 de octubre de 2026. Base revisada: `52be6dd` de `main`.

## Problema y causa

El validador oficial está en `data_pipeline/validador.py`, pero el panel seguía
importando `validador` desde la raíz. En un clon limpio, Streamlit interrumpía
el arranque con `ModuleNotFoundError: No module named 'validador'`.

## Corrección

`ui/app.py` importa ahora `validar_lote_crudo` desde `data_pipeline.validador`.
Se reutilizan las reglas de Data Engineering; no se agrega otra implementación
ni se modifican los contratos Pydantic.

La prueba `test_panel_arranca_sin_cargar_un_lote` ejecuta el punto de entrada con
AppTest y comprueba que aparezcan el título inicial y la acción de datos de
ejemplo sin excepciones. Complementa el recorrido de revisión ya existente.

## Evidencia reproducible

Entorno: Windows, Python 3.11.16, dependencias de `uv.lock`, Streamlit 1.63.0.

```powershell
uv sync --frozen
uv run --frozen pytest tests/test_frontend.py::test_panel_arranca_sin_cargar_un_lote -q
uv run --frozen pytest -q
git diff --check
```

- Antes de corregir el import: la prueba nueva falla por `No module named 'validador'`.
- Después: la suite completa termina con **26 passed, 1 deselected**.
- La prueba deseleccionada corresponde a integración OCI real, excluida por
  la configuración de pytest; no se contabiliza como aprobada.
- `git diff --check` sin errores.

Esta evidencia verifica ejecución con AppTest, no una revisión visual nueva
ni una operación contra OCI real. No requiere credenciales.

## Relación con los acuerdos del 9 de octubre

Esta corrección desbloquea el panel existente para la integración E2E (#10)
y aporta evidencia a la consolidación documental (#11).

En la base revisada todavía no existen `data_pipeline/db.py` ni
`orchestration/pipeline_runner.py`. La integración del historial y la auditoría
SQLite queda pendiente de la entrega de Andrea y de las interfaces coordinadas
con Leidalí. El panel sigue usando análisis y generación de demostración y
almacenamiento local. Este PR no afirma completar la integración E2E.

El siguiente aporte frontend deberá consumir los datos persistidos y reflejar
`status_almacenamiento`, `comprobacion_lectura` y reintentos del flujo oficial,
sin duplicar ingesta, validación ni persistencia en la interfaz.
