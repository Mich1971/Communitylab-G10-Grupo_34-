# Especificación Técnica y Plan de Implementación: `ai_modules/cognitive_analyzer.py`

**Proyecto:** CommunityLab — Hackathon ONE G10 (Oracle Next Education & Alura) · Equipo 34

**Componente:** Agente de Análisis Cognitivo (Contrato 2 / Tarea S1-1 / Requisito R2)

**Responsable:** Michael Antonio Chacón Rossell — AI Engineer

**Fecha:** 13 de octubre de 2026

**Destinatario:** Antigravity (Google) / Squad de Desarrollo CommunityLab

---

## 1. Resumen Ejecutivo y Diagnóstico de Estado

El archivo `ai_modules/cognitive_analyzer.py` constituye el núcleo computacional del Agente de Análisis Cognitivo y representa el entregable central de la Fase 2 del plan de trabajo. Actualmente, el módulo **no ha sido codificado en el repositorio**; únicamente existen las piezas desacopladas de soporte e infraestructura (`instructions.py`, `catalog.py`, `multiprovider_agent.py`, `test_calibration_eval.py` y `validation_ground_truth.json`).

Este documento establece la especificación técnica definitiva, subsana las ambigüedades arquitectónicas previas (delimitación estricta de fronteras frente a `validador.py` y desacoplamiento de `storage/oci_client.py`), formaliza las firmas de interfaz y detalla la lógica de resiliencia operativa y agregación estadística. Su objetivo es servir como contexto directivo ejecutable en Antigravity para la construcción inmediata del módulo.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        FLUJO ARQUITECTÓNICO MVP                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  [data/raw/ JSON]                                                      │
│         │                                                              │
│         ▼                                                              │
│  data_pipeline/validador.py                                            │
│    ├── Admisión y parseo de sobre                                      │
│    ├── Aislamiento de texto vacío / errores de esquema                 │
│    └── Desvío a SQLite (data/communitylab.db)                          │
│         │                                                              │
│         ▼ List[InteraccionValidada] (Garantizada limpia)               │
│                                                                        │
│  ai_modules/cognitive_analyzer.py  ◄── [ESTE MÓDULO]                   │
│    ├── Enmascaramiento PII / Truncamiento preventivo                   │
│    ├── Inferencia LLM (PydanticAI + Prompt Caching)                   │
│    ├── Resiliencia operativa (Backoff + Degradación Elegante)          │
│    └── Agregación estadística -> AnalisisCognitivo                     │
│         │                                                              │
│         ▼ AnalisisCognitivo (Contrato 2)                               │
│                                                                        │
│  orchestration/pipeline_runner.py (LangGraph)                          │
│    ├── Bifurcación condicional (Logro / Duda / Dificultad)             │
│    ├── Invocación de ai_modules/copy_generator.py                      │
│    └── Persistencia en OCI Object Storage vía storage/oci_client.py    │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘

```

---

## 2. Delimitación de Fronteras y Principios de Diseño

Para preservar una arquitectura desacoplada y orientada a capas, `cognitive_analyzer.py` opera bajo los siguientes límites de responsabilidad:

1. **Frontera de Entrada (Desacoplamiento de `validador.py`):**
El módulo **no realiza validación sintáctica de mensajes crudos ni gestiona el descarte de textos vacíos hacia SQLite**. Dicha tarea ya es ejecutada en la Capa 2 por `data_pipeline/validador.py` (`validar_lote_crudo`). `cognitive_analyzer.py` asume como precondición invariable que toda entrada es una lista tipada de `InteraccionValidada`.


2. **Naturaleza Funcional Pura (Sin llamadas directas a OCI):**
El agente cognitivo es una función transformadora sin estado. **No interactúa directamente con `storage/oci_client.py` ni persiste en Object Storage**. Su salida es estrictamente una instancia validada del modelo Pydantic `AnalisisCognitivo`. La coordinación de la persistencia en la nube corresponde al orquestador general de la máquina de estados en `orchestration/pipeline_runner.py`.


3. **Resiliencia y Degradación Elegante (*Semantic Fallback*):**
Ante indisponibilidad de la API externa, agotamiento de cuota (HTTP 429) o fallos irrecuperables en el parseo del esquema JSON, el módulo no aborta la ejecución del lote. Aplica una degradación controlada registro por registro: asigna las dimensiones de `PuntuacionRelevancia` en $0$, marca `apto_para_publicacion = False`, documenta la causa en `motivo_seleccion` y **preserva de forma inmutable el `source_id` original**.



---

## 3. Especificación Formal de Interfaces y Contratos

El módulo debe ubicarse en `ai_modules/cognitive_analyzer.py` y exponer la siguiente interfaz pública:

```python
from typing import List, Optional
import structlog
from esquemas import AnalisisCognitivo, InteraccionValidada

logger = structlog.get_logger("ai_modules.cognitive_analyzer")


async def analizar_lote_cognitivo(
    interacciones: List[InteraccionValidada],
    run_id: str,
    request_id: str,
    provider: Optional[str] = None,
) -> AnalisisCognitivo:
  """Punto de entrada principal para el análisis cognitivo de un lote de interacciones.

  Args:
      interacciones: Lista de mensajes previamente validados por
        data_pipeline.validador.
      run_id: Identificador único de ejecución del pipeline (ej.
        'run-20261013-a8f1').
      request_id: Identificador de trazabilidad del payload de admisión.
      provider: Proveedor LLM opcional ('openai', 'gemini', 'anthropic'). Si es
        None, se resuelve a través de la configuración del entorno.

  Returns:
      AnalisisCognitivo: Instancia canónica del Contrato 2 que contiene el
      resumen estadístico y la lista de mensajes analizados.
  """
  ...

```

### Funciones Auxiliares Internas Requeridas

* `_enmascarar_pii(texto: str) -> str`: Sanitiza patrones evidentes de información personal (correos electrónicos, números telefónicos y credenciales/tokens) mediante expresiones regulares antes de transmitir el texto a la API del modelo de lenguaje.
* `_truncar_texto_semantico(texto: str, max_chars: int = 2000) -> str`: Si un mensaje excede el umbral por volcados extensos de logs o código, preserva el $20\%$ inicial (contexto del usuario) y el $80\%$ final (desenlace/error), truncando la sección intermedia.


* `_ejecutar_inferencia_con_resiliencia(...) -> MensajeAnalizado`: Gestiona la llamada al agente de PydanticAI con retroceso exponencial (*exponential backoff with jitter*). Si se agotan los reintentos, genera la instancia de degradación elegante.


* `_calcular_resumen_comunidad(...) -> ResumenComunidad`: Agrega las métricas deterministas del lote mediante funciones puras de Python, garantizando la total conformidad con el esquema canónico.



---

## 4. Lógica de Agregación y Cumplimiento de `ResumenComunidad`

Para evitar inconsistencias de esquema con `esquemas.py`, los campos de `ResumenComunidad` deben calcularse por software de manera determinista al finalizar la inferencia del lote:

```python
from collections import Counter
from typing import Dict, List
from esquemas import MensajeAnalizado, ResumenComunidad


def _calcular_resumen_comunidad(
    mensajes: List[MensajeAnalizado], total_recibidos_en_lote: int
) -> ResumenComunidad:
  """Calcula de forma determinista el resumen estadístico del lote analizado."""
  total_procesados = len(mensajes)

  # Conteo de registros válidos vs degradados/fallidos
  degradados = sum(
      1
      for m in mensajes
      if m.puntuacion_relevancia.total == 0
      and "Degradación por error" in m.motivo_seleccion
  )
  validos = total_procesados - degradados

  # Distribución y moda de sentimiento sobre las 4 clases oficiales
  conteo_sentimientos: Dict[str, int] = {
      "Altamente Positivo": 0,
      "Positivo": 0,
      "Neutro": 0,
      "Dificultad/Negativo": 0,
  }
  for m in mensajes:
    if m.sentimiento in conteo_sentimientos:
      conteo_sentimientos[m.sentimiento] += 1

  sentimiento_moda = (
      max(conteo_sentimientos, key=conteo_sentimientos.get)
      if total_procesados > 0
      else "Neutro"
  )

  # Consolidación plana de temas frecuentes
  todos_los_temas = [t for m in mensajes for t in m.temas]
  temas_top = [
      tema
      for tema, _ in Counter(todos_los_temas).most_common(5)  # type: ignore
  ]
  if not temas_top:
    temas_top = ["General"]

  # Extracción de alertas de soporte
  alertas = [
      f"[{m.source_id}] {m.motivo_seleccion}"
      for m in mensajes
      if m.requiere_soporte
  ]

  return ResumenComunidad(
      total_interacciones_procesadas=total_recibidos_en_lote,
      registros_validos=validos,
      registros_rechazados=degradados,
      sentimiento_predominante=sentimiento_moda,
      distribucion_sentimiento=conteo_sentimientos,
      temas_principales=temas_top,
      alertas_soporte=alertas,
  )

```

---

## 5. Arquitectura de Resiliencia Operativa y Observabilidad

### Mecanismo de Reintentos y Degradación Elegante

1. **Reintentos a Temperatura Cero:**
Ante excepciones de validación de esquema en la salida (`pydantic.ValidationError`), el agente debe reintentar hasta 2 veces forzando temperatura $0.0$ para reducir la variabilidad estocástica y favorecer el cumplimiento de la estructura JSON.


2. **Backoff con Jitter:**
Ante errores de red o límites de tasa de la API (códigos HTTP 429 o 503), se aplica un retroceso exponencial con variación aleatoria:

$$t_{\text{espera}} = \min(t_{\text{max}},\, t_{\text{base}} \times 2^{\text{intento}}) \pm \text{uniform}(0, 0.5)$$


3. **Generación del Fallback:**
Si se agotan los reintentos para un mensaje específico, se emite una instancia de contingencia sin romper el lote:
```python
MensajeAnalizado(
    source_id=interaccion.id,  # Trazabilidad estricta preservada
    sentimiento="Neutro",
    categoria_enrutamiento="Duda",
    regla_aplicada="Degradación elegante por indisponibilidad de inferencia",
    temas=["Sin_Clasificar"],
    entidades_relevantes=[],
    puntuacion_relevancia=PuntuacionRelevancia(
        evidencia_explicita=0,
        utilidad_comunitaria=0,
        claridad_contexto=0,
        total=0,
    ),
    motivo_seleccion=(
        f"Degradación por error de servicio tras reintentos: {error_str}"
    ),
    requiere_soporte=False,
    apto_para_publicacion=False,
)

```



```

### Telemetría con `structlog`

El módulo debe emitir eventos en formato JSON estructurado registrando la latencia y el consumo de tokens para verificar el cumplimiento del SLA ($\le 1.5\text{ s}$ por registro en lotes estándar)[cite: 13]:

```python
logger.info(
    "analisis_cognitivo_completado",
    run_id=run_id,
    request_id=request_id,
    total_procesados=len(mensajes),
    latencia_total_ms=round(tiempo_total * 1000, 2),
    latencia_promedio_por_registro_ms=round(
        (tiempo_total / len(mensajes)) * 1000, 2
    ),
    degradados=degradados,
    tokens_cacheados=tokens_cacheados,
)

```

---

## 6. Estado de los Componentes de Soporte Existentes

El desarrollo de `cognitive_analyzer.py` se apoya en los siguientes artefactos ya construidos y verificados en el repositorio:

| Componente | Rol en el Pipeline | Estado Actual |
| --- | --- | --- |
| `ai_modules/prompts/instructions.py` | Directivas maestras, taxonomía de sentimiento y reglas de enrutamiento | Completado |
| `ai_modules/prompts/catalog.py` | Constante `CATALOGO_FEW_SHOT` con 15 ejemplos canónicos equilibrados | Completado |
| `ai_modules/multiprovider_agent.py` | `CognitiveAgentFactory` con soporte de hot-swapping y prompt caching | Completado |
| `data/samples/validation_ground_truth.json` | Dataset sintético de referencia para verificación empírica de métricas | Completado |
| `tests/test_calibration_eval.py` | Arnés de pruebas para Cohen's Kappa, Macro $F_1$, Exactitud y MAE | Completado |

---

## 7. Plan de Implementación por Fases (Fases Restantes)

### Fase 2/3: Construcción de `cognitive_analyzer.py` (Prioridad Inmediata)

* **Objetivo:** Codificar el módulo orquestador integrando sanitización PII, invocación a `CognitiveAgentFactory`, reintentos, fallback elegante y agregación de `ResumenComunidad`.


* **Entregable:** Archivo funcional `ai_modules/cognitive_analyzer.py` verificado contra `pyright` y `ruff`.



### Fase 3: Pruebas Unitarias Aisladas (Sin Consumo de API Externa)

* **Objetivo:** Garantizar la cobertura lógica de los mecanismos internos sin requerir claves de API activas.


* **Entregable:** Archivo `tests/test_cognitive_analyzer_unit.py` verificando:
1. Preservación estricta del `source_id` en flujos nominales y de degradación.


2. Enmascaramiento correcto de correos electrónicos y teléfonos en `_enmascarar_pii`.
3. Cálculo de la moda de sentimiento y conteo exacto de las 4 clases en `_calcular_resumen_comunidad`.


4. Activación de reintentos y fallback simulando fallos mediante mocks del cliente LLM.





### Fase 4: Evaluación Empírica y Calibración Semántica

* **Objetivo:** Ejecutar la suite `test_calibration_eval.py` contra credenciales reales de API para validar que el catálogo alcance los umbrales exigidos para el MVP:


* Concordancia de enrutamiento (Cohen's Kappa): $\kappa \ge 0.80$
* Exactitud global de análisis de sentimiento: $\ge 85.0\%$
* Error Absoluto Medio (MAE) de relevancia: $\le 0.75$ puntos
* Trazabilidad de identificadores: $100\%$ coincidencia de `source_id`


* **Entregable:** Ejecución documentada en verde de la prueba de integración y generación del reporte técnico de calibración.



### Fase 5: Integración en el Pipeline y Despliegue

* **Objetivo:** Conectar el módulo en el grafo de ejecución de LangGraph (`orchestration/pipeline_runner.py`) para transferir `AnalisisCognitivo` hacia la generación de copys editoriales (`copy_generator.py`) y posterior persistencia en OCI.


* **Entregable:** Pipeline de punta a punta verificado con la suite completa de integración.



---

## 8. Observaciones y Recomendaciones para la Implementación (Antigravity & Engineering Squad)

1. **Gestión de Dependencias en `pyproject.toml`:**
El arnés de evaluación empírica (`tests/test_calibration_eval.py`) hace uso de `scikit-learn` y `scipy`. Es imperativo agregarlas formalmente al grupo de desarrollo ejecutando `uv add --dev scikit-learn scipy` para regenerar `uv.lock` y evitar fallos de importación en entornos limpios o en integración continua.


2. **Dimensionamiento del Catálogo y Techo de Tokens de Caché:**
Al interactuar con `gemini-1.5-flash`, el umbral de activación de caché de prefijo puede demandar hasta 2,048 tokens dependiendo de la configuración del endpoint. Debe verificarse mediante telemetría que el prefijo estático (`SYSTEM_PROMPT` + `CATALOGO_FEW_SHOT`) supere dicho margen en el tokenizador de Google para evitar la facturación de lecturas frías repetitivas.


3. **Parametrización Multi-Proveedor en Pruebas de Integración:**
Actualmente, el arnés de calibración ejecuta la prueba únicamente contra el proveedor por defecto (`OpenAI`). Se recomienda parametrizar la prueba (`@pytest.mark.parametrize("provider", ["openai", "gemini", "anthropic"])`) para auditar la consistencia semántica y el hot-swapping de los tres proveedores previstos en la arquitectura.


4. **Respeto a la Invariante de Retorno en LangGraph:**
El nodo de LangGraph en `orchestration/pipeline_runner.py` debe recibir el objeto `AnalisisCognitivo` emitido por este módulo y colocarlo en el estado global del grafo (`state["analisis_cognitivo"]`). Bajo ninguna circunstancia debe intentarse guardar el análisis de forma prematura en disco o en la nube dentro del análisis cognitivo.


5. **Priorización de Entrega MVP:**
Mecanismos avanzados como el particionamiento dinámico en micro-lotes por agotamiento de cuota deben implementarse de forma sencilla y no bloqueante (por ejemplo, procesamiento secuencial de mensajes o concurrencia limitada con `asyncio.gather(..., return_exceptions=True)`), priorizando la estabilidad del flujo punta a punta sobre optimizaciones complejas de concurrencia.