# Pruebas negativas PN-001 y PN-002 · ingesta idempotente

Fecha: 25 de octubre de 2026. Issue relacionado: HU-S1-006 (#10), sección
«Pruebas negativas y de resiliencia». Ejecutadas por Sergio contra la API real
(`uvicorn api.main:app`), sin worker y con datos sintéticos.

## Resumen

| Prueba | Resultado |
|---|---|
| **PN-001 · Reenvío idéntico** | ✅ Cumple: el backend devuelve el mismo `run_id` con `duplicado: true` y **no** crea un segundo procesamiento |
| **PN-002 · Conflicto de identificador** | ✅ Cumple: el backend responde `409 conflicto_idempotencia` y **no** sobrescribe el lote original |

Ambas coinciden con el comportamiento definido por el backend (`api/main.py` y
`api/registro.py`). No se encontraron fallos.

## Cómo define el backend «duplicado» y «conflicto»

- **Identificador:** el `request_id` del sobre de ingesta.
- **Huella:** SHA-256 del JSON canónico del lote ya validado (claves ordenadas,
  sin espacios). Por eso el orden de las claves y el formato (compacto o con
  sangría) no cambian la huella.
- **Mismo `request_id` + misma huella** → duplicado: `200`, mismo `run_id`.
- **Mismo `request_id` + distinta huella** → conflicto: `409`, no se escribe nada.
- **`request_id` nuevo** → lote nuevo: `202`, aunque el contenido sea idéntico a otro.

## Entorno

- API en un puerto local, con directorio de datos limpio y sin worker, para que el
  lote quedara en `data/raw/` y se pudieran contar archivos.
- Lote: `data/raw/lote_prueba_01.json` (4 interacciones) con `request_id`
  `req-pn-demo-001`.
- Evidencia cruda: [`pn/pn001-pn002-evidencia.json`](pn/pn001-pn002-evidencia.json).
  Script para repetirla: [`pn/reproducir_pn001_pn002.py`](pn/reproducir_pn001_pn002.py).

## PN-001 · Reenvío idéntico

Objetivo del issue: reenviar el mismo lote, comprobar el comportamiento definido
para duplicados e **evitar procesamiento duplicado no deseado**.

| # | Acción | HTTP | Respuesta | Archivos en `raw/` |
|---|---|---|---|---|
| 1 | Primer envío | `202` | `estado: en_cola`, `duplicado: false` | 1 |
| 2 | Reenvío idéntico | `200` | **mismo** `run_id`, `duplicado: true` | 1 |
| 3 | Mismo contenido, otro orden de claves y con sangría | `200` | mismo `run_id`, `duplicado: true` | 1 |
| 4 | 8 reenvíos idénticos en paralelo | `200` (los 8) | mismo `run_id`, `duplicado: true` | 1 |
| 5 | Estado en la base | — | 1 sola fila en `solicitudes_ingesta` para ese `request_id` | — |

**Reenvío de un lote ya procesado** (usando el run real de la prueba E2E, en una
copia del directorio de datos):

| Lote reenviado | HTTP | Respuesta |
|---|---|---|
| Run completado | `200` | mismo `run_id`, `estado: completado`, `duplicado: true` |
| Run fallido | `200` | mismo `run_id`, `estado: fallido`, `duplicado: true` |

En ambos casos `raw/` siguió vacío: no se volvió a encolar nada.

**Conclusión PN-001:** el reenvío no genera un segundo run ni un segundo archivo,
ni siquiera con 8 peticiones simultáneas. La respuesta refleja el estado actual del
run.

## PN-002 · Conflicto de identificador

Objetivo del issue: enviar un lote con un identificador existente pero información
diferente, y verificar que el conflicto se detecta y se maneja.

Se usó el `request_id` ya registrado `req-pn-demo-001` con tres variaciones:

| # | Variación | HTTP | Respuesta |
|---|---|---|---|
| 1 | Un mensaje con el texto modificado | `409` | `conflicto_idempotencia` |
| 2 | Otro `periodo_referencia` | `409` | `conflicto_idempotencia` |
| 3 | Un mensaje menos | `409` | `conflicto_idempotencia` |

Mensaje devuelto: «request_id ya registrado con contenido distinto; no se
sobrescribe».

Comprobaciones posteriores:

- La base sigue con **1** fila para ese `request_id` y su **huella no cambió**.
- `raw/` sigue con **1** archivo: el lote original no se tocó.
- El run original sigue en `en_cola`, sin alteraciones.

**Contraste:** el mismo contenido con otro `request_id` (`req-pn-demo-001-b`) se
acepta como lote nuevo (`202`, nuevo `run_id`). La deduplicación es por
`request_id`, no por contenido.

**Conclusión PN-002:** el conflicto se detecta, se rechaza con un error tipado y no
hay efecto sobre los datos existentes.

## Correspondencia con los criterios del issue #10

- [x] Se comprueba el comportamiento ante reenvío idéntico.
- [x] Se comprueba el comportamiento ante conflicto de ID.
- [x] Existe evidencia reproducible de la ejecución (JSON + script).

## Pruebas automáticas que ya cubren lo mismo

En `tests/test_api_main.py`:

- `test_repeticion_identica_devuelve_200_con_el_mismo_run_id_sin_tocar_disco`
- `test_orden_de_claves_y_schema_version_implicito_no_cambian_la_huella`
- `test_carrera_de_peticiones_identicas_crea_un_solo_run`
- `test_mismo_request_id_con_otro_contenido_devuelve_409_y_no_sobrescribe`
- `test_carrera_con_contenidos_distintos_deja_un_ganador_y_409_al_resto`

Esta evidencia complementa esas pruebas: las ejecuta contra la API levantada de
verdad, con HTTP real, y deja constancia del estado de los archivos y de la base.

## Limitaciones y observaciones

- **Un lote fallido no se reintenta reenviándolo.** Con el mismo `request_id`, el
  backend devuelve `200` con `estado: fallido` y no lo vuelve a procesar. Para
  reintentar hay que enviar un `request_id` nuevo. Es coherente con la idempotencia,
  pero conviene que el equipo lo tenga presente (por ejemplo, el botón «Enviar datos
  de ejemplo» del panel usa un `request_id` fijo: tras el primer envío siempre
  devolverá el mismo run).
- **No cubierto aquí:** el caso `503 recepcion_en_curso` (dos recepciones
  simultáneas de un lote aún no terminado de escribir) y los identificadores
  inválidos (`422 identificadores_invalidos`). Están cubiertos por pruebas
  automáticas, no por esta ejecución en vivo.
- **Interpretación de «identificador»:** se tomó como `request_id` del sobre, que es
  la clave de idempotencia del backend. No se probó la repetición de IDs de
  interacción dentro de un mismo lote.

## Cómo repetirlo

```powershell
$env:COMMUNITYLAB_DATA_DIR = "<directorio-vacio>"
$env:COMMUNITYLAB_WEBHOOK_SECRET = "<secreto-local-de-16-o-mas-caracteres>"
uv run --frozen uvicorn api.main:app --host 127.0.0.1 --port 8047
# en otra terminal, con el mismo directorio y secreto:
uv run --frozen python docs/evidencias/pn/reproducir_pn001_pn002.py `
  http://127.0.0.1:8047 "<directorio-vacio>" "<secreto>" data/raw/lote_prueba_01.json salida.json
```

El script usa el `request_id` del archivo que se le pasa, así que el directorio de
datos debe estar vacío (o el archivo debe traer un `request_id` que no exista ya);
si no, el primer envío no será nuevo. En la ejecución documentada se usó una copia de
`lote_prueba_01.json` con `request_id` `req-pn-demo-001`. No arrancar el worker si se
quiere contar archivos en `raw/`; el script no usa IA ni OCI.
