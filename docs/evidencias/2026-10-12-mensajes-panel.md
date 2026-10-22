# Mensajes y orientación del panel

Este aporte ayuda a recorrer las cuatro pantallas sin confundir una revisión
humana con una publicación o con almacenamiento real en OCI.

## Qué cambia y por qué

- Todas las pantallas muestran el paso actual y explican que análisis y
  generación son simulados, y que el almacenamiento es local.
- Carga confirma cuántos mensajes pueden continuar aun con rechazos. Si un
  archivo nuevo falla, aclara que el lote anterior sigue disponible. El error
  original del validador se conserva como diagnóstico técnico, con ajuste de línea.
- Resultados recuerda revisar las fuentes y que la clasificación usa reglas locales.
- Edición explica los estados pendiente, rechazado y aprobado, los requisitos
  para registrar la decisión y que aprobar no publica ni guarda en OCI.
- Almacenamiento distingue el paquete pendiente de guardar de la copia local
  verificada, y explica cómo descargarlo o volver a editar.

## Alcance

Se mantiene la validación oficial y el flujo existente. No se agregan tablas,
adaptadores, llamadas a OCI ni interfaces provisionales para los módulos de
Andrea y Leidalí. El indicador de paso describe navegación, no progreso real
del backend. La sesión sigue siendo temporal.

La rama parte de la corrección de arranque del PR #17, pendiente de integración.
Revisar ese PR primero; este aporte complementa su cambio.

## Verificación

Resultado local del 12 de octubre: **26 passed, 1 deselected** (Python 3.11,
dependencias de `uv.lock`). `git diff --check` terminó sin errores.

Ejecutar `uv run --frozen pytest -q` para comprobar el arranque, el recorrido de
revisión y la invalidación de la aprobación al editar. La prueba de OCI real
queda excluida por la configuración del proyecto y requiere su propia evidencia.

Para revisar los textos manualmente, ejecutar `uv run streamlit run ui/app.py`:

1. Cargar los datos de ejemplo y comprobar los 3 válidos y 1 rechazado.
2. Continuar al análisis y preparar los borradores de demostración.
3. Intentar aprobar sin revisor; después rechazar con revisor y motivo.
4. Ajustar el texto, aprobar y comprobar el aviso de guardado pendiente.
5. Guardar la copia local y comprobar que ningún mensaje acredita uso de OCI.

Esta lista es una guía de revisión manual, no una afirmación de ejecución visual.
