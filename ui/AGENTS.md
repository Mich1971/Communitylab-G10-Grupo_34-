# Capa de Interfaz y Curaduría (Streamlit) — Notas para Agentes

Este archivo define las directrices para la construcción y mantenimiento del panel de curaduría humana en Streamlit (`ui/app.py` y `ui/components/`). Consulta previamente las directrices generales en [../AGENTS.md](../AGENTS.md).

---

## 1. Propósito del Módulo de UI

La interfaz de Streamlit cumple el **Requisito R5** del proyecto: proporciona una consola visual donde el gestor de comunidad puede:
1. Inspeccionar métricas y analítica del lote procesado (distribución de sentimiento, temas clave, alertas de soporte).
2. Auditar la veracidad factual de los copys generados mediante la visualización directa de los mensajes fuente vinculados (`source_ids`).
3. Editar el contenido propuesto para LinkedIn y el resumen semanal.
4. Emitir un veredicto formal (`aprobado` o `rechazado`) e incrementar la versión del paquete inmutable en OCI Object Storage.
5. Comprobar visualmente el estado de persistencia y la confirmación de lectura en la nube (`comprobacion_lectura`).

---

## 2. Stack y Arquitectura de la Interfaz

* **Tecnología:** Streamlit (`>=1.63,<1.64`) ejecutado bajo Python 3.11 con `uv`.
* **Entrada de Datos:** 
  * Lectura de analítica y auditoría desde la base de datos local SQLite (`data/communitylab.db`).
  * Deserialización de paquetes de distribución mediante los contratos de `esquemas.py`.
* **Integración con Persistencia:** Conexión con `storage/oci_client.py` para cargar versiones históricas y publicar nuevas revisiones.

---

## 3. Organización de Archivos en `ui/`

```text
ui/
├── AGENTS.md                  # Este archivo de instrucciones
├── app.py                     # Punto de entrada principal y enrutador de vistas
├── components/
│   ├── header_metrics.py      # Tarjetas de analítica comunitaria y alertas
│   ├── copy_curator.py        # Editores de texto para LinkedIn y Resumen con citas
│   ├── source_inspector.py    # Visualizador de mensajes de origen vinculados por ID
│   └── oci_badge.py           # Badge visual de estado y verificación de hash OCI
└── styles/
    └── custom.css             # Estilos CSS ligeros para insignias y resaltado
```

---

## 4. Reglas Operativas y Flujo de Interacción

### 4.1 Cero Lógica de Negocio en la UI
* La interfaz **no ejecuta llamadas directas a modelos LLM** ni implementa heurísticas de scoring de relevancia.
* Consume exclusivamente los objetos `AnalisisCognitivo` y `PaqueteSalida` o las vistas provistas por SQLite.

### 4.2 Máquina de Estados de Curaduría Humana
* El curador opera sobre el objeto `EstadoRevision`:
  * `estado`: `Literal["pendiente", "aprobado", "rechazado"]`.
  * `numero_revision`: Entero incremental estricto ($1, 2, 3, \dots$).
  * `revisor`: Nombre o identificador del usuario que aprueba/edita.
  * `comentarios`: Justificación editorial opcional del cambio.
  * `fecha_decision`: Marca de tiempo UTC del veredicto.

### 4.3 Inmutabilidad Estricta de Revisiones (Directiva M02 / M07)
* Cuando el usuario edita un texto y pulsa **«Aprobar y Sincronizar en OCI»**:
  1. La aplicación **nunca sobrescribe** el archivo `revision-001.json` original.
  2. Incrementa el contador a `numero_revision = revision_actual + 1`.
  3. Preserva intacta la lista obligatoria de `source_ids`.
  4. Invoca al método `upload_package()` de `storage/oci_client.py`.
  5. Actualiza la vista con el resultado de la comprobación activa de lectura (`comprobacion_lectura: bool`).

### 4.4 Garantía de Trazabilidad Visual
* En el editor de copys, los `source_ids` no deben ser removidos ni puestos en blanco por error del curador. Si una edición vacía la lista de fuentes, el botón de guardado debe deshabilitarse o emitir una advertencia en pantalla, preservando el principio de "cero hechos inventados".

---

## 5. Prácticas de Rendimiento en Streamlit

* **Uso de Caché Controlada:** Usa `@st.cache_data` únicamente para consultas de lectura en SQLite o descarga de paquetes inmutables previos de OCI. Invalida la caché explícitamente tras registrar una nueva revisión.
* **Gestión de Sesión:** Centraliza el estado activo del lote seleccionado en `st.session_state` (`current_run_id`, `active_revision_number`).
* **Protección ante Recargas:** El flujo debe tolerar refrescos de página sin perder la selección del lote actual, recuperando el último estado persistido desde SQLite.

---

## 6. Anti-Patrones Rechazados en la Capa UI

* Incorporar frameworks frontend adicionales (React, Node, TypeScript) en esta capa: el MVP utiliza Streamlit nativo.
* Realizar mutaciones destructivas en disco o base de datos sin confirmación visual previa.
* Ocultar el estado de error de OCI si la comprobación activa de lectura falla; siempre se debe reflejar si el paquete quedó en `guardado_error` para permitir el reintento.
* Permitir la aprobación de activos que carezcan de referencias a mensajes originales (`source_ids`).