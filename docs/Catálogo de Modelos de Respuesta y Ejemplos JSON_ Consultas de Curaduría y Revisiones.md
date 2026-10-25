# Catálogo de Modelos de Respuesta y Ejemplos JSON: Consultas de Curaduría y Revisiones (Fase 0)

**Proyecto:** CommunityLab — Hackathon ONE G10 (Oracle Next Education & Alura) · Equipo 34  
**Componente:** Interfaz de Consultas de Lectura, Revisiones Transaccionales y Guardado en OCI (`storage/consultas.py`, `storage/revisiones.py` y `api/rutas_curaduria.py`)  
**Destinatario:** Sergio Rebolledo (Frontend Developer — Panel de Curaduría en Streamlit)  
**Autores / Coordinación Backend:** Michael Chacón, Enrique Reyes, Andrea Camarena  
**Fecha:** 24 de octubre de 2026 · Cierre de Fase 0 (Congelamiento de Interfaz)

---

## 1. Propósito y Convenciones Generales

Este documento congela los **modelos Pydantic de respuesta/petición** y proporciona **ejemplos JSON exactos** para las **10 operaciones** definidas en el *«Plan de implementación: consultas de curaduría y revisiones»*, permitiendo que el panel de Streamlit (`ui/`) se conecte en paralelo sin depender de listas vacías ambiguas.

### Reglas de Oro de la Interfaz (HTTP y Python)
1. **Paridad 1:1 Python ↔ HTTP:** Cada operación existe como función en `storage/consultas.py` o `storage/revisiones.py` y como endpoint bajo `/api/v1/...` en `api/rutas_curaduria.py`, recibiendo los mismos parámetros y devolviendo exactamente el mismo modelo Pydantic serializado.
2. **Autenticación Uniforme:** Todas las rutas bajo `/api/v1` exigen la cabecera `X-Webhook-Secret: <COMMUNITYLAB_WEBHOOK_SECRET>`. Si falta o no coincide, devuelve HTTP `401`.
3. **Sobre de Disponibilidad Explícita (`disponible` y `motivo`):** Ninguna consulta por `run_id` deja a la UI adivinando por qué una lista vino vacía. Toda respuesta de sub-recurso (`mensajes`, `fuentes`, `resumen`, `rechazados`, `revisiones`) incluye:
   * `estado`: Estado operativo exacto (`"recibiendo"`, `"en_cola"`, `"procesando"`, `"completado"`, `"sin_registros_validos"`, `"sin_contenido_publicable"`, `"fallido"`).
   * `categoria_estado`: Agrupador semántico para la UI (`"en_curso"`, `"finalizada_con_paquete"`, `"finalizada_sin_paquete"`, `"fallida"`).
   * `disponible`: `bool` (`true` si existen datos consultables para esa vista; `false` si aún no están listos o no aplican).
   * `motivo`: `Optional[Literal["en_curso", "sin_paquete", "sin_analisis"]]` (`null` cuando `disponible=true`).
4. **Cero Silencios ante Fallos de Base de Datos:** Si SQLite lanza cualquier excepción durante una lectura, el backend **jamás** devuelve `[]`; responde HTTP `503` con `{"error": "lectura_fallida", "detalle": "..."}`.

---

## 2. Modelos Pydantic de Respuesta y Petición

A continuación se definen los modelos Pydantic v2 que respaldan las 10 operaciones (reutilizando `PuntuacionRelevancia`, `ResumenComunidad`, `PostLinkedIn`, `ResumenSemanal`, `EvidenciaAlmacenamientoOCI` y `PaqueteSalida` de `esquemas.py` sin alterarlos):

```python
from datetime import datetime
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from esquemas import (
    CategoriaEnrutamiento,
    EvidenciaAlmacenamientoOCI,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
    Sentimiento,
)

# ---------------------------------------------------------------------------
# Tipos de Estado y Categorías para la UI (Sección 3 del Plan)
# ---------------------------------------------------------------------------
EstadoOperativo = Literal[
    "recibiendo",
    "en_cola",
    "procesando",
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
]

CategoriaEstado = Literal[
    "en_curso",
    "finalizada_con_paquete",
    "finalizada_sin_paquete",
    "fallida",
]

MotivoNoDisponible = Literal["en_curso", "sin_paquete", "sin_analisis"]


class SobreEstadoRun(BaseModel):
    """Encabezado común incluido en todas las consultas asociadas a un run_id."""
    run_id: str
    request_id: str
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    disponible: bool
    motivo: Optional[MotivoNoDisponible] = None


# ---------------------------------------------------------------------------
# 1. listar_ejecuciones (GET /api/v1/runs)
# ---------------------------------------------------------------------------
class ItemEjecucionResumen(BaseModel):
    run_id: str
    request_id: str
    fecha_recepcion: str
    fecha_procesamiento: Optional[str] = None
    comunidad: str
    periodo: str
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    tiene_paquete: bool


class RespuestaListarEjecuciones(BaseModel):
    items: List[ItemEjecucionResumen]
    siguiente_cursor: Optional[str] = None
    total: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# 2. obtener_ejecucion (GET /api/v1/runs/{run_id}/detalle)
# ---------------------------------------------------------------------------
class RespuestaDetalleEjecucion(BaseModel):
    run_id: str
    request_id: str
    comunidad: str
    periodo: str
    fecha_recepcion: str
    fecha_procesamiento: Optional[str] = None
    estado: EstadoOperativo
    categoria_estado: CategoriaEstado
    estado_flujo: Optional[str] = None
    total_recibidos: int = Field(default=0, ge=0)
    total_validos: int = Field(default=0, ge=0)
    total_rechazados: int = Field(default=0, ge=0)
    tiene_paquete: bool
    revision_vigente: Optional[int] = None


# ---------------------------------------------------------------------------
# 3. listar_mensajes (GET /api/v1/runs/{run_id}/mensajes)
# ---------------------------------------------------------------------------
class ItemMensajeAnalizadoUI(BaseModel):
    source_id: str
    categoria_enrutamiento: CategoriaEnrutamiento
    sentimiento: Sentimiento
    puntuacion: PuntuacionRelevancia
    motivo_seleccion: str
    regla_aplicada: str
    temas: List[str]
    entidades: List[str]
    requiere_soporte: bool
    apto_para_publicacion: bool


class RespuestaListarMensajes(SobreEstadoRun):
    items: List[ItemMensajeAnalizadoUI] = Field(default_factory=list)
    siguiente_cursor: Optional[str] = None
    total: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# 4. obtener_fuentes (GET /api/v1/runs/{run_id}/fuentes?source_ids=a,b)
# ---------------------------------------------------------------------------
class ItemFuenteOriginal(BaseModel):
    source_id: str
    autor: str
    canal: str
    tipo: str
    texto: str
    tipo_texto: Literal["original_validado"] = "original_validado"


class RespuestaObtenerFuentes(SobreEstadoRun):
    items: List[ItemFuenteOriginal] = Field(default_factory=list)
    no_encontrados: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5. obtener_resumen (GET /api/v1/runs/{run_id}/resumen)
# ---------------------------------------------------------------------------
class RespuestaObtenerResumen(SobreEstadoRun):
    resumen: Optional[ResumenComunidad] = None


# ---------------------------------------------------------------------------
# 6. listar_rechazados (GET /api/v1/runs/{run_id}/rechazados)
# ---------------------------------------------------------------------------
class ItemRechazadoUI(BaseModel):
    mensaje_id: Optional[str] = None
    autor: Optional[str] = None
    canal: Optional[str] = None
    motivo: str
    origen: Literal["validador", "analisis"]
    fecha_aislamiento: str
    texto_crudo: Optional[str] = None


class RespuestaListarRechazados(SobreEstadoRun):
    items: List[ItemRechazadoUI] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# 7. listar_revisiones (GET /api/v1/runs/{run_id}/revisiones)
# ---------------------------------------------------------------------------
class ItemRevisionResumen(BaseModel):
    numero_revision: int = Field(..., ge=1)
    estado: Literal["pendiente", "aprobado", "rechazado"]
    revisor: Optional[str] = None
    comentarios: Optional[str] = None
    fecha_decision: Optional[str] = None
    fecha_generacion: str
    status_almacenamiento: Literal["guardado_con_exito", "guardado_error", "no_iniciado"]
    comprobacion_lectura: bool


class RespuestaListarRevisiones(SobreEstadoRun):
    revision_vigente: Optional[int] = None
    items: List[ItemRevisionResumen] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 8. obtener_revision (GET /api/v1/runs/{run_id}/revisiones/{n})
# ---------------------------------------------------------------------------
class RespuestaObtenerRevision(BaseModel):
    run_id: str
    numero_revision: int
    paquete: PaqueteSalida
    evidencia_oci: EvidenciaAlmacenamientoOCI


# ---------------------------------------------------------------------------
# 9. registrar_revision (POST /api/v1/runs/{run_id}/revisiones)
# ---------------------------------------------------------------------------
class EdicionRevisionInput(BaseModel):
    estado: Literal["pendiente", "aprobado", "rechazado"]
    revisor: str = Field(..., min_length=1)
    comentarios: Optional[str] = None
    post_linkedin: Optional[PostLinkedIn] = None
    resumen_semanal: Optional[ResumenSemanal] = None


class SolicitudRegistrarRevision(BaseModel):
    revision_base: int = Field(..., ge=1, description="Última revisión que el curador tiene en pantalla")
    edicion: EdicionRevisionInput


class RespuestaRegistrarRevision(BaseModel):
    run_id: str
    numero_revision: int
    estado: Literal["pendiente", "aprobado", "rechazado"]
    ruta_objeto: str


# ---------------------------------------------------------------------------
# 10. guardar_revision_en_oci (POST /api/v1/runs/{run_id}/revisiones/{n}/oci)
# ---------------------------------------------------------------------------
class RespuestaGuardarOCI(BaseModel):
    run_id: str
    numero_revision: int
    bucket: str
    ruta_objeto: str
    status_almacenamiento: Literal["guardado_con_exito", "guardado_error"]
    comprobacion_lectura: bool
    mensaje_error: Optional[str] = None
    reintento_idempotente: bool = False


# ---------------------------------------------------------------------------
# Formato Estándar de Error HTTP (401, 404, 409, 422, 503)
# ---------------------------------------------------------------------------
class ErrorAPI(BaseModel):
    error: str
    detalle: str
    revision_actual: Optional[int] = None  # Presente en 409 conflicto_revision
```

---

## 3. Matriz Rápida de Estados y Comportamiento para el Frontend

Antes de revisar los JSON por operación, esta tabla resume qué valores exactos recibe Streamlit en el sobre de cada respuesta según el ciclo de vida del `run_id`:

| Situación del Lote | `estado` | `categoria_estado` | `tiene_paquete` | `mensajes` / `resumen` (`disponible`, `motivo`) | `revisiones` (`disponible`, `motivo`) |
| :--- | :--- | :--- | :---: | :--- | :--- |
| **1. Todavía procesando** | `"recibiendo"` \| `"en_cola"` \| `"procesando"` | `"en_curso"` | `false` | `disponible: false`<br>`motivo: "en_curso"` | `disponible: false`<br>`motivo: "en_curso"` |
| **2. Completado con éxito** | `"completado"` | `"finalizada_con_paquete"` | `true` | `disponible: true`<br>`motivo: null` | `disponible: true`<br>`motivo: null` |
| **3A. Finalizado sin contenido publicable** *(solo dudas/quejas)* | `"sin_contenido_publicable"` | `"finalizada_sin_paquete"` | `false` | `disponible: true`<br>`motivo: null` *(hay análisis para explorar)* | `disponible: false`<br>`motivo: "sin_paquete"` |
| **3B. Finalizado sin registros válidos** *(100% rechazado en validador)* | `"sin_registros_validos"` | `"finalizada_sin_paquete"` | `false` | `disponible: false`<br>`motivo: "sin_analisis"` *(pero `rechazados` tiene `disponible: true`)* | `disponible: false`<br>`motivo: "sin_paquete"` |
| **4. Fallido** *(caída total del LLM o excepción técnica)* | `"fallido"` | `"fallida"` | `false` | Si dejó análisis degradado: `disponible: true`, `categoria_estado: "fallida"`.<br>Si cayó antes: `disponible: false`, `motivo: "sin_analisis"`. | `disponible: false`<br>`motivo: "sin_paquete"` |

---

## 4. Ejemplos JSON por Operación (Casos Nominales, Sin Paquete y Errores)

### Operación 1: `listar_ejecuciones`
* **Ruta HTTP:** `GET /api/v1/runs?limite=20&estado=&periodo=&comunidad=&cursor=`
* **Uso en UI:** Poblar el selector principal o tabla histórica de ejecuciones en la barra lateral / vista de inicio, mostrando de un vistazo cuáles tienen paquete listo para curar, cuáles siguen en curso y cuáles terminaron sin paquete.

#### ✅ Ejemplo 200 OK (Histórico con los 4 estados representados)
```json
{
  "items": [
    {
      "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
      "request_id": "req_semana04_001",
      "fecha_recepcion": "2026-10-24T18:30:00.120Z",
      "fecha_procesamiento": "2026-10-24T18:30:04.850Z",
      "comunidad": "Discord_Grupo_ONE_G10",
      "periodo": "Semana_04",
      "estado": "completado",
      "categoria_estado": "finalizada_con_paquete",
      "tiene_paquete": true
    },
    {
      "run_id": "run-20261024T184500Z-112233445566",
      "request_id": "req_semana04_002",
      "fecha_recepcion": "2026-10-24T18:45:00.050Z",
      "fecha_procesamiento": null,
      "comunidad": "Discord_Grupo_ONE_G10",
      "periodo": "Semana_04",
      "estado": "procesando",
      "categoria_estado": "en_curso",
      "tiene_paquete": false
    },
    {
      "run_id": "run-20261024T171000Z-998877665544",
      "request_id": "req_soporte_003",
      "fecha_recepcion": "2026-10-24T17:10:00.000Z",
      "fecha_procesamiento": "2026-10-24T17:10:03.210Z",
      "comunidad": "Discord_Grupo_ONE_G10",
      "periodo": "Semana_04",
      "estado": "sin_contenido_publicable",
      "categoria_estado": "finalizada_sin_paquete",
      "tiene_paquete": false
    },
    {
      "run_id": "run-20261024T160000Z-ffeeddccbbaa",
      "request_id": "req_vacio_004",
      "fecha_recepcion": "2026-10-24T16:00:00.000Z",
      "fecha_procesamiento": "2026-10-24T16:00:00.410Z",
      "comunidad": "Discord_Grupo_ONE_G10",
      "periodo": "Semana_04",
      "estado": "sin_registros_validos",
      "categoria_estado": "finalizada_sin_paquete",
      "tiene_paquete": false
    },
    {
      "run_id": "run-20261024T150000Z-001122334455",
      "request_id": "req_caida_005",
      "fecha_recepcion": "2026-10-24T15:00:00.000Z",
      "fecha_procesamiento": "2026-10-24T15:00:12.900Z",
      "comunidad": "Discord_Grupo_ONE_G10",
      "periodo": "Semana_04",
      "estado": "fallido",
      "categoria_estado": "fallida",
      "tiene_paquete": false
    }
  ],
  "siguiente_cursor": null,
  "total": 5
}
```

#### ❌ Ejemplo 422 Unprocessable Entity (`limite` fuera de rango `1..100`)
```json
{
  "error": "parametro_invalido",
  "detalle": "El parámetro 'limite' debe estar entre 1 y 100 (recibido: 500)."
}
```

---

### Operación 2: `obtener_ejecucion`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/detalle`
* **Uso en UI:** Cargar las tarjetas superiores de estado del lote (volumetría de recibidos, válidos, rechazados y revisión activa). **Funciona tanto si hay paquete como si no hay paquete.**

#### ✅ Caso A: Ejecución `completado` (con paquete y revisión vigente) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "comunidad": "Discord_Grupo_ONE_G10",
  "periodo": "Semana_04",
  "fecha_recepcion": "2026-10-24T18:30:00.120Z",
  "fecha_procesamiento": "2026-10-24T18:30:04.850Z",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "estado_flujo": "completado",
  "total_recibidos": 4,
  "total_validos": 3,
  "total_rechazados": 1,
  "tiene_paquete": true,
  "revision_vigente": 2
}
```

#### ⏳ Caso B: Ejecución `en_curso` (todavía en cola o procesando) — `200 OK`
```json
{
  "run_id": "run-20261024T184500Z-112233445566",
  "request_id": "req_semana04_002",
  "comunidad": "Discord_Grupo_ONE_G10",
  "periodo": "Semana_04",
  "fecha_recepcion": "2026-10-24T18:45:00.050Z",
  "fecha_procesamiento": null,
  "estado": "procesando",
  "categoria_estado": "en_curso",
  "estado_flujo": "iniciado",
  "total_recibidos": 3,
  "total_validos": 3,
  "total_rechazados": 0,
  "tiene_paquete": false,
  "revision_vigente": null
}
```

#### ⚠️ Caso C: Ejecución `finalizada_sin_paquete` (`sin_contenido_publicable`) — `200 OK`
```json
{
  "run_id": "run-20261024T171000Z-998877665544",
  "request_id": "req_soporte_003",
  "comunidad": "Discord_Grupo_ONE_G10",
  "periodo": "Semana_04",
  "fecha_recepcion": "2026-10-24T17:10:00.000Z",
  "fecha_procesamiento": "2026-10-24T17:10:03.210Z",
  "estado": "sin_contenido_publicable",
  "categoria_estado": "finalizada_sin_paquete",
  "estado_flujo": "sin_contenido_publicable",
  "total_recibidos": 2,
  "total_validos": 2,
  "total_rechazados": 0,
  "tiene_paquete": false,
  "revision_vigente": null
}
```

#### ❌ Caso D: Ejecución inexistente — `404 Not Found`
```json
{
  "error": "ejecucion_no_encontrada",
  "detalle": "No existe ninguna ejecución con run_id='run-20261024T000000Z-noexiste0000'."
}
```

---

### Operación 3: `listar_mensajes`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/mensajes?apto=&categoria=&sentimiento=&requiere_soporte=&limite=50&cursor=`
* **Uso en UI:** Tabla de exploración analítica por mensaje (sentimiento, categoría, desglose 0-6 de relevancia, justificación y regla aplicada).

#### ✅ Caso A: Con análisis disponible (`completado` o `sin_contenido_publicable`) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "disponible": true,
  "motivo": null,
  "items": [
    {
      "source_id": "msg_001",
      "categoria_enrutamiento": "Logro",
      "sentimiento": "Altamente Positivo",
      "puntuacion": {
        "evidencia_explicita": 2,
        "utilidad_comunitaria": 2,
        "claridad_contexto": 2,
        "total": 6
      },
      "motivo_seleccion": "Comunica un hito laboral verificable como Desarrolladora Junior de IA y detalla el impacto del portfolio con LangChain y OCI.",
      "regla_aplicada": "Resultado laboral concreto vinculado con evidencia de aprendizaje.",
      "temas": ["empleabilidad", "LangChain", "OCI"],
      "entidades": ["LangChain", "OCI", "Desarrolladora Junior de IA"],
      "requiere_soporte": false,
      "apto_para_publicacion": true
    },
    {
      "source_id": "msg_002",
      "categoria_enrutamiento": "Duda",
      "sentimiento": "Neutro",
      "puntuacion": {
        "evidencia_explicita": 1,
        "utilidad_comunitaria": 2,
        "claridad_contexto": 1,
        "total": 4
      },
      "motivo_seleccion": "Plantea una consulta técnica clara sobre nodos condicionales y reintentos en LangGraph sin bloqueo grave.",
      "regla_aplicada": "Pregunta técnica explícita sobre arquitectura de agentes.",
      "temas": ["LangGraph", "nodos condicionales"],
      "entidades": ["LangGraph"],
      "requiere_soporte": false,
      "apto_para_publicacion": false
    }
  ],
  "siguiente_cursor": null,
  "total": 2
}
```

#### ⏳ Caso B: Ejecución todavía procesando (`en_curso`) — `200 OK`
```json
{
  "run_id": "run-20261024T184500Z-112233445566",
  "request_id": "req_semana04_002",
  "estado": "procesando",
  "categoria_estado": "en_curso",
  "disponible": false,
  "motivo": "en_curso",
  "items": [],
  "siguiente_cursor": null,
  "total": 0
}
```

#### ⚠️ Caso C: Ejecución sin análisis (`sin_registros_validos`: todo el lote fue rechazado por el validador) — `200 OK`
```json
{
  "run_id": "run-20261024T160000Z-ffeeddccbbaa",
  "request_id": "req_vacio_004",
  "estado": "sin_registros_validos",
  "categoria_estado": "finalizada_sin_paquete",
  "disponible": false,
  "motivo": "sin_analisis",
  "items": [],
  "siguiente_cursor": null,
  "total": 0
}
```

#### 🔴 Caso D: Ejecución `fallida` por caída total del LLM (deja mensajes degradados en SQLite, pero `categoria_estado="fallida"` avisa a la UI que no es un análisis normal) — `200 OK`
```json
{
  "run_id": "run-20261024T150000Z-001122334455",
  "request_id": "req_caida_005",
  "estado": "fallido",
  "categoria_estado": "fallida",
  "disponible": true,
  "motivo": null,
  "items": [
    {
      "source_id": "msg_001",
      "categoria_enrutamiento": "Dificultad",
      "sentimiento": "Neutro",
      "puntuacion": {
        "evidencia_explicita": 0,
        "utilidad_comunitaria": 0,
        "claridad_contexto": 0,
        "total": 0
      },
      "motivo_seleccion": "Degradación por error de servicio tras reintentos: TimeoutError",
      "regla_aplicada": "Degradación elegante por indisponibilidad de inferencia",
      "temas": ["Sin_Clasificar"],
      "entidades": [],
      "requiere_soporte": true,
      "apto_para_publicacion": false
    }
  ],
  "siguiente_cursor": null,
  "total": 1
}
```

---

### Operación 4: `obtener_fuentes`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/fuentes?source_ids=msg_001,msg_002,msg_999`
* **Uso en UI:** Inspector de veracidad factual. Cuando el curador revisa un borrador de LinkedIn o del Resumen Semanal, la UI toma la lista `source_ids` del activo y llama a esta operación para mostrar el texto original exacto (`tipo_texto = "original_validado"`). Si algún ID no existe en ese `run_id`, aparece en `no_encontrados`.

#### ✅ Caso A: Fuentes encontradas (con un ID inexistente reportado en `no_encontrados`) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "disponible": true,
  "motivo": null,
  "items": [
    {
      "source_id": "msg_001",
      "autor": "Mariana Souza",
      "canal": "#logros-y-empleos",
      "tipo": "testimonio",
      "texto": "Comunidad, quedé seleccionada para el puesto de Desarrolladora Junior de IA! El proyecto del curso de LangChain y OCI que construí en mi portfolio marcó toda la diferencia en la entrevista técnica.",
      "tipo_texto": "original_validado"
    },
    {
      "source_id": "msg_002",
      "autor": "Lucas Albuquerque",
      "canal": "#dudas-langgraph",
      "tipo": "pregunta_tecnica",
      "texto": "Tengo dudas sobre cómo estructurar los nodos condicionales en LangGraph cuando la respuesta del LLM necesita reintento.",
      "tipo_texto": "original_validado"
    }
  ],
  "no_encontrados": ["msg_999"]
}
```

#### ❌ Caso B: Parámetro `source_ids` vacío o con más de 50 IDs — `422 Unprocessable Entity`
```json
{
  "error": "parametro_invalido",
  "detalle": "Debe especificar entre 1 y 50 identificadores en 'source_ids'."
}
```

---

### Operación 5: `obtener_resumen`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/resumen`
* **Uso en UI:** Poblar los gráficos de distribución de sentimiento, temas principales de la semana y alertas de soporte comunitario.

#### ✅ Caso A: Resumen disponible (`completado` o `sin_contenido_publicable`) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "disponible": true,
  "motivo": null,
  "resumen": {
    "total_interacciones_procesadas": 3,
    "registros_validos": 3,
    "registros_rechazados": 0,
    "sentimiento_predominante": "Positivo",
    "distribucion_sentimiento": {
      "Altamente Positivo": 1,
      "Positivo": 1,
      "Neutro": 1,
      "Dificultad/Negativo": 0
    },
    "temas_principales": ["OCI", "LangGraph", "empleabilidad", "Pydantic"],
    "alertas_soporte": []
  }
}
```

#### ⚠️ Caso B: Sin resumen porque el lote no tuvo registros válidos (`sin_registros_validos`) — `200 OK`
```json
{
  "run_id": "run-20261024T160000Z-ffeeddccbbaa",
  "request_id": "req_vacio_004",
  "estado": "sin_registros_validos",
  "categoria_estado": "finalizada_sin_paquete",
  "disponible": false,
  "motivo": "sin_analisis",
  "resumen": null
}
```

---

### Operación 6: `listar_rechazados`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/rechazados?origen=&incluir_texto=false`
* **Uso en UI:** Tabla de auditoría de registros aislados por el validador (Capa 2: textos vacíos, fechas corruptas) o durante el análisis. Por privacidad (Decisión D1), `texto_crudo` viene `null` a menos que se solicite explícitamente `incluir_texto=true`.

#### ✅ Caso A: Con registros rechazados (`incluir_texto=false` por defecto) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "disponible": true,
  "motivo": null,
  "items": [
    {
      "mensaje_id": "msg_004",
      "autor": "Usuario Invalido",
      "canal": "#soporte",
      "motivo": "El texto de la interacción está vacío o contiene solo espacios.",
      "origen": "validador",
      "fecha_aislamiento": "2026-10-24T18:30:00.310Z",
      "texto_crudo": null
    }
  ],
  "total": 1
}
```

#### ✅ Caso B: Lote limpio sin ningún rechazo — `200 OK`
```json
{
  "run_id": "run-20261024T171000Z-998877665544",
  "request_id": "req_soporte_003",
  "estado": "sin_contenido_publicable",
  "categoria_estado": "finalizada_sin_paquete",
  "disponible": true,
  "motivo": null,
  "items": [],
  "total": 0
}
```

---

### Operación 7: `listar_revisiones`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/revisiones`
* **Uso en UI:** Mostrar el historial de versiones inmutables del paquete (`revision-002`, `revision-001`) en orden descendente, indicando cuál es la `revision_vigente` y el estado de su verificación de lectura en OCI.

#### ✅ Caso A: Ejecución `completado` con dos revisiones (borrador inicial + edición aprobada) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "request_id": "req_semana04_001",
  "estado": "completado",
  "categoria_estado": "finalizada_con_paquete",
  "disponible": true,
  "motivo": null,
  "revision_vigente": 2,
  "items": [
    {
      "numero_revision": 2,
      "estado": "aprobado",
      "revisor": "sergio.rebolledo",
      "comentarios": "Se ajustó el llamado a la acción del post de LinkedIn y se verificaron las citas.",
      "fecha_decision": "2026-10-24T19:05:10Z",
      "fecha_generacion": "2026-10-24T19:05:10Z",
      "status_almacenamiento": "guardado_con_exito",
      "comprobacion_lectura": true
    },
    {
      "numero_revision": 1,
      "estado": "pendiente",
      "revisor": null,
      "comentarios": null,
      "fecha_decision": null,
      "fecha_generacion": "2026-10-24T18:30:04Z",
      "status_almacenamiento": "guardado_con_exito",
      "comprobacion_lectura": true
    }
  ]
}
```

#### ⚠️ Caso B: Ejecución sin paquete (`sin_contenido_publicable`, `sin_registros_validos` o `fallido`) — `200 OK`
> **Importante para la UI:** Aquí `disponible` es `false` y `motivo` es `"sin_paquete"`. Con esto Streamlit deshabilita el editor de copys y muestra el aviso informativo («Este lote no generó paquete publicable porque ningún mensaje calificó como apto para difusión pública»).
```json
{
  "run_id": "run-20261024T171000Z-998877665544",
  "request_id": "req_soporte_003",
  "estado": "sin_contenido_publicable",
  "categoria_estado": "finalizada_sin_paquete",
  "disponible": false,
  "motivo": "sin_paquete",
  "revision_vigente": null,
  "items": []
}
```

#### ⏳ Caso C: Ejecución todavía `en_curso` — `200 OK`
```json
{
  "run_id": "run-20261024T184500Z-112233445566",
  "request_id": "req_semana04_002",
  "estado": "procesando",
  "categoria_estado": "en_curso",
  "disponible": false,
  "motivo": "en_curso",
  "revision_vigente": null,
  "items": []
}
```

---

### Operación 8: `obtener_revision`
* **Ruta HTTP:** `GET /api/v1/runs/{run_id}/revisiones/{numero_revision}`
* **Uso en UI:** Cargar el Contrato 3 completo (`PaqueteSalida` v1.2.0 reconstruido desde SQLite) para poblar los campos editables de `PostLinkedIn`, `ResumenSemanal`, la lista de `casos_no_publicables`, el badge de `evidencia_oci` y permitir la descarga directa en JSON/MD/TXT.

#### ✅ Caso A: Revisión encontrada — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "numero_revision": 1,
  "paquete": {
    "schema_version": "1.2.0",
    "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
    "request_id": "req_semana04_001",
    "fecha_generacion": "2026-10-24T18:30:04Z",
    "metadatos_ejecucion": {
      "modelo": "gpt-4o-mini + gpt-4o-mini",
      "prompt_version": "1.0.0+f82a91c0 + generacion-1.0.1+sha256:4a8b9c1d2e3f",
      "latencia_ms": 1420,
      "tokens_totales": 1150
    },
    "analisis_resumido": {
      "total_interacciones_procesadas": 3,
      "registros_validos": 3,
      "registros_rechazados": 0,
      "sentimiento_predominante": "Positivo",
      "distribucion_sentimiento": {
        "Altamente Positivo": 1,
        "Positivo": 1,
        "Neutro": 1,
        "Dificultad/Negativo": 0
      },
      "temas_principales": ["OCI", "LangGraph", "empleabilidad"],
      "alertas_soporte": []
    },
    "activos": {
      "post_linkedin": {
        "titulo": "De la práctica en comunidad al primer rol en IA",
        "canal_recomendado": "LinkedIn Oficial",
        "cuerpo": "Esta semana en la comunidad ONE G10 celebramos un nuevo logro profesional: una integrante fue seleccionada como Desarrolladora Junior de IA tras presentar en su entrevista técnica el proyecto práctico construido con LangChain y OCI.",
        "hashtags": ["#OCI", "#LangChain", "#Empleabilidad", "#ComunidadONE"],
        "source_ids": ["msg_001"]
      },
      "resumen_semanal": {
        "seccion": "Actividad comunitaria",
        "titular": "Resumen semanal: avances en OCI y arquitectura con LangGraph",
        "resumen": "Durante la Semana 04 la comunidad compartió hitos de inserción laboral apalancados en portafolios sobre OCI y LangChain, además de consultas técnicas sobre el diseño de nodos condicionales y reintentos en LangGraph.",
        "source_ids": ["msg_001", "msg_002"]
      },
      "casos_no_publicables": [
        {
          "source_id": "msg_002",
          "sentimiento": "Neutro",
          "categoria_enrutamiento": "Duda",
          "temas": ["LangGraph", "nodos condicionales"],
          "requiere_soporte": false,
          "motivo_seleccion": "Plantea una consulta técnica clara sobre nodos condicionales y reintentos en LangGraph sin bloqueo grave."
        }
      ]
    },
    "revision": {
      "numero_revision": 1,
      "estado": "pendiente",
      "revisor": null,
      "fecha_decision": null,
      "comentarios": null
    },
    "almacenamiento_oci": {
      "bucket": "communitylab-activos-marketing",
      "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-001.json",
      "status_almacenamiento": "guardado_con_exito",
      "comprobacion_lectura": true
    }
  },
  "evidencia_oci": {
    "bucket": "communitylab-activos-marketing",
    "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-001.json",
    "status_almacenamiento": "guardado_con_exito",
    "comprobacion_lectura": true
  }
}
```

#### ❌ Caso B: El `run_id` existe pero no tiene esa revisión (o es un lote sin paquete) — `404 Not Found`
```json
{
  "error": "revision_no_encontrada",
  "detalle": "No existe la revisión 3 para run_id='run-20261024T183000Z-a1b2c3d4e5f6'."
}
```

---

### Operación 9: `registrar_revision`
* **Ruta HTTP:** `POST /api/v1/runs/{run_id}/revisiones`
* **Uso en UI:** Guardar de forma transaccional una nueva revisión inmutable (`revision_base + 1`) cuando el curador edita los textos, aprueba (incluso sin editar, Decisión D3) o rechaza el borrador.
* **Regla de Concurrencia Optimista:** El frontend envía `revision_base` (el número de la última revisión que cargó en pantalla). Si otro revisor o pestaña ya creó una revisión superior, el backend responde `409 conflicto_revision` indicando `revision_actual`.

#### 📤 Ejemplo de Cuerpo de Petición (`Request Body`)
> Nota: `post_linkedin` y `resumen_semanal` son opcionales en `edicion`; si el curador aprueba sin tocar el texto, se pueden omitir (o enviar idénticos) y el backend copiará los activos de `revision_base`.
```json
{
  "revision_base": 1,
  "edicion": {
    "estado": "aprobado",
    "revisor": "sergio.rebolledo",
    "comentarios": "Aprobado tras ajustar el titular de LinkedIn.",
    "post_linkedin": {
      "titulo": "Del portafolio técnico en OCI y LangChain a Desarrolladora Junior de IA",
      "canal_recomendado": "LinkedIn Oficial",
      "cuerpo": "Esta semana en la comunidad ONE G10 celebramos un nuevo logro profesional: una integrante fue seleccionada como Desarrolladora Junior de IA tras presentar en su entrevista técnica el proyecto práctico construido con LangChain y OCI.",
      "hashtags": ["#OCI", "#LangChain", "#Empleabilidad", "#ComunidadONE"],
      "source_ids": ["msg_001"]
    },
    "resumen_semanal": null
  }
}
```

#### ✅ Caso A: Revisión creada exitosamente — `201 Created`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "numero_revision": 2,
  "estado": "aprobado",
  "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-002.json"
}
```

#### ❌ Caso B: Conflicto de concurrencia (`revision_base` desactualizada) — `409 Conflict`
```json
{
  "error": "conflicto_revision",
  "detalle": "La revisión base enviada (1) no coincide con la última revisión registrada (2). Recargue antes de guardar.",
  "revision_actual": 2
}
```

#### ❌ Caso C: Violación de trazabilidad factual o validación (p. ej. `source_ids` vacío, ID inexistente o cuerpo < 20 caracteres) — `422 Unprocessable Entity`
```json
{
  "error": "revision_invalida",
  "detalle": "post_linkedin.source_ids fuera del conjunto de mensajes aptos: ['msg_002']"
}
```

#### ❌ Caso D: Intento de crear revisión en un `run_id` que no tiene paquete inicial (`sin_contenido_publicable`) — `404 Not Found`
```json
{
  "error": "revision_no_encontrada",
  "detalle": "La ejecución 'run-20261024T171000Z-998877665544' no cuenta con un paquete base sobre el cual registrar revisiones."
}
```

---

### Operación 10: `guardar_revision_en_oci`
* **Ruta HTTP:** `POST /api/v1/runs/{run_id}/revisiones/{numero_revision}/oci`
* **Uso en UI:** Sincronizar en OCI Object Storage la revisión recién creada (o reintentar una revisión cuyo estado era `guardado_error` o `no_iniciado`) verificando la lectura activa byte a byte (SHA-256).
* **Idempotencia:** Si la revisión `(run_id, numero_revision)` ya tenía `status_almacenamiento == "guardado_con_exito"` y `comprobacion_lectura == true`, devuelve `200 OK` con `reintento_idempotente: true`.

#### ✅ Caso A: Guardado y comprobación de lectura exitosos en OCI — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "numero_revision": 2,
  "bucket": "communitylab-activos-marketing",
  "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-002.json",
  "status_almacenamiento": "guardado_con_exito",
  "comprobacion_lectura": true,
  "mensaje_error": null,
  "reintento_idempotente": false
}
```

#### ✅ Caso B: Llamada repetida sobre una revisión ya sincronizada (Reintento idempotente) — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "numero_revision": 2,
  "bucket": "communitylab-activos-marketing",
  "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-002.json",
  "status_almacenamiento": "guardado_con_exito",
  "comprobacion_lectura": true,
  "mensaje_error": null,
  "reintento_idempotente": true
}
```

#### ⚠️ Caso C: Fallo de conexión o credenciales de OCI (Aislamiento M07: no lanza 500, conserva la revisión en SQLite y devuelve `guardado_error` para que la UI habilite el botón «Reintentar sincronización OCI») — `200 OK`
```json
{
  "run_id": "run-20261024T183000Z-a1b2c3d4e5f6",
  "numero_revision": 2,
  "bucket": "communitylab-activos-marketing",
  "ruta_objeto": "activos/Semana_04/run-20261024T183000Z-a1b2c3d4e5f6/revision-002.json",
  "status_almacenamiento": "guardado_error",
  "comprobacion_lectura": false,
  "mensaje_error": "Credenciales OCI no disponibles en el entorno",
  "reintento_idempotente": false
}
```

---

### Errores Transversales a Todas las Operaciones (`401` y `503`)

#### 🔒 Error 401 Unauthorized (Falta o es inválido `X-Webhook-Secret`)
```json
{
  "error": "no_autorizado",
  "detalle": "X-Webhook-Secret ausente o inválido"
}
```

#### 🗄️ Error 503 Service Unavailable (Fallo de lectura/bloqueo en SQLite — Nunca devuelve lista vacía)
```json
{
  "error": "lectura_fallida",
  "detalle": "No se pudo consultar la base de datos local SQLite; reintente en unos segundos."
}
```

---

## 5. Secuencia Recomendada de Llamadas desde Streamlit (`ui/app.py`)

1. **Al abrir el panel o refrescar:** Llamar a `listar_ejecuciones(limite=20)` para poblar el selector de ejecuciones.
2. **Al seleccionar un `run_id`:** Llamar a `obtener_ejecucion(run_id)`:
   * Si `categoria_estado == "en_curso"`, mostrar indicador de progreso y refrescar.
   * Si `categoria_estado != "en_curso"`, cargar las pestañas llamando a `obtener_resumen(run_id)`, `listar_mensajes(run_id)` y `listar_rechazados(run_id)`.
   * Si `tiene_paquete == true`, llamar a `listar_revisiones(run_id)` y a `obtener_revision(run_id, revision_vigente)` para poblar el editor de curaduría, y a `obtener_fuentes(run_id, source_ids=...)` para el panel lateral de citas originales.
3. **Al pulsar «Aprobar / Guardar Revisión»:**
   * Paso 1: Invocar `registrar_revision(run_id, revision_base=revision_vigente, edicion=...)` $\rightarrow$ recibe `nuevo_numero = 2`.
   * Paso 2: Invocar `guardar_revision_en_oci(run_id, numero_revision=nuevo_numero)` $\rightarrow$ actualiza el badge visual de OCI (`guardado_con_exito` + `comprobacion_lectura: true`, o botón de reintento si devuelve `guardado_error`).