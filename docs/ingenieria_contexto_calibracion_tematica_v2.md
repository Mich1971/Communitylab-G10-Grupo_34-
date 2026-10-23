# Ingeniería de contexto para la calibración temática de agentes cognitivos mediante catálogos de demostración

**Proyecto CommunityLab — Hackathon ONE G10 (Equipo 34)**
**Componente:** Agente de Análisis Cognitivo (`ai_modules/cognitive_analyzer.py` / `ai_modules/multiprovider_agent.py`) — Tarea S1-1 · Requisito R2 · Directiva M10
**Versión:** 2 — consolidada a partir de los cuatro informes previos del equipo (fundamentos de ingeniería de contexto, arquitectura de caché multi-proveedor, selección de LLM, dimensionamiento del catálogo) y del arnés de evaluación empírica ya implementado.

---

## 1. Resumen ejecutivo

Este documento integra, en una sola metodología coherente y lista para implementarse, las cuatro piezas que el equipo desarrolló por separado:

1. Los fundamentos de ingeniería de contexto y aprendizaje *many-shot* en modelos de lenguaje.
2. La arquitectura de *prompt caching* multi-proveedor (OpenAI, Anthropic, Google Gemini) y su implementación en PydanticAI.
3. El dimensionamiento concreto del catálogo `CATALOGO_FEW_SHOT` (15 a 17 ejemplos) con su presupuesto de tokens.
4. El marco de validación empírica (conjunto *hold-out*, prueba A/B con significancia estadística, matriz de umbrales de aceptación) que determina si el catálogo realmente calibra al agente.

La sección 10 cierra con las observaciones de implementación pendientes de verificación y los riesgos residuales que el equipo debe resolver antes de considerar esta metodología definitiva.

---

## 2. Fundamentos de la ingeniería de contexto y el régimen *many-shot*

La evolución desde la formulación artesanal de instrucciones hacia la ingeniería de contexto obedece a la necesidad de gestionar el espacio de atención de los modelos de lenguaje como un recurso computacional finito, dinámico y sujeto a degradación sistemática. Mientras que el diseño convencional de instrucciones se limita a articular directivas lingüísticas aisladas, la ingeniería de contexto comprende la selección algorítmica, estructuración, compresión y gobierno de la totalidad de fichas léxicas que coexisten simultáneamente en la ventana del transformador durante el ciclo de inferencia de un agente.

En agentes cognitivos orientados a la interpretación de comunidades digitales, el principal desafío estriba en la entropía discursiva de los canales conversacionales, caracterizados por intervenciones fragmentadas, jerga técnica emergente, elisiones contextuales deliberadas y cargas emotivas heterogéneas. El aprendizaje en contexto con escasas demostraciones (tres a ocho ejemplos) resulta insuficiente para modelar esta distribución, forzando al modelo a depender de sesgos semánticos y *priors* adquiridos durante su preentrenamiento.

La disponibilidad de ventanas de contexto a gran escala (128k–1M+ tokens en arquitecturas contemporáneas) ha permitido la adopción formal del régimen con catálogos demostrativos amplios. La investigación sobre *many-shot in-context learning* muestra que incrementar sustancialmente el número de demostraciones — hasta decenas o cientos de ejemplos, en tareas abiertas de razonamiento o traducción — transforma de forma cualitativa el comportamiento inductivo del sistema, mitigando sesgos preexistentes y estabilizando la distribución predictiva.

**Matiz aplicado a CommunityLab (ver sección 5):** esa investigación evalúa tareas abiertas de razonamiento, donde el catálogo debe enseñar tanto el formato de salida como el criterio de decisión. En un pipeline con PydanticAI, el formato ya está garantizado matemáticamente por *constrained decoding* contra el esquema `AnalisisCognitivo` — el catálogo aquí solo necesita calibrar el criterio semántico de clasificación, no la sintaxis. Esta diferencia de objetivo es la que justifica operar con decenas de ejemplos y no con los cientos que esa literatura evalúa para tareas abiertas.

El riesgo del lado opuesto es real: el escalado descontrolado de demostraciones introduce saturación atencional y degradación contextual progresiva. A medida que el denominador de la función *softmax* de atención acumula pares clave-valor redundantes o ruidosos, la probabilidad asignada a directivas procedimentales críticas decae:

$$\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V$$

Por este motivo, la integración de catálogos de ejemplos provenientes de plataformas dinámicas (Discord, foros) exige una arquitectura de ingeniería de contexto fundamentada en la sanitización sintáctica, el muestreo estratificado y la separación deliberada de capas estáticas y dinámicas — los tres ejes que desarrollan las secciones siguientes.

---

## 3. Curaduría, normalización y estructuración del corpus de origen

### 3.1 Normalización sintáctica y sanitización de ruido comunicacional

Las conversaciones en Discord y plataformas colaborativas incorporan artefactos sintácticos propios de su infraestructura de entrega: identificadores opacos de usuario, menciones a roles del servidor, emoticonos personalizados, hipervínculos efímeros y trazas de depuración de código incompletas. La preparación del catálogo exige aplicar canalizaciones deterministas de limpieza antes de consolidar cualquier texto demostrativo: traducir las etiquetas de plataforma a roles discursivos estandarizados, normalizar los bloques de código bajo convenciones legibles de Markdown, y eliminar volcados excesivos de registros que consumirían presupuesto contextual sin aportar valor interpretativo.

La totalidad del catálogo debe atravesar un proceso de enmascaramiento mediante expresiones regulares y reconocimiento de entidades para neutralizar nombres reales, correos electrónicos, números telefónicos o claves de autenticación. Este enmascaramiento **mitiga, pero no garantiza**, la ausencia de información identificable: regex y NER tienen una tasa de falsos negativos no trivial frente a apodos, menciones sin `@` o jerga informal propia de Discord — debe tratarse como una capa de reducción de riesgo, no como una garantía absoluta de anonimización, y conviene una revisión manual puntual del catálogo final antes de fijarlo como constante inmutable del sistema.

### 3.2 Selección estratificada del catálogo y cobertura de casos límite

Un catálogo demostrativo calibrado no persigue la acumulación exhaustiva de registros, sino la máxima densidad de cobertura semántica sobre los límites categóricos del sistema. La selección debe mitigar activamente el sesgo de clase mayoritaria: en comunidades de desarrollo, las preguntas técnicas rutinarias dominan numéricamente el canal, lo que puede inducir al modelo a etiquetar erróneamente cualquier consulta como neutra. Por ello el catálogo incorpora, de forma forzada, una proporción equilibrada entre `Logro`, `Duda` y `Dificultad` (ver distribución exacta en la sección 5), y casos frontera explícitos: mensajes que combinan agradecimientos con quejas metodológicas, consultas con instrucciones ambiguas diseñadas para poner a prueba la resistencia del agente ante intentos de inyección de directivas, y dilemas técnicos que exigen la máxima puntuación en evidencia objetiva.

### 3.3 Formateo canónico entrada-salida alineado a contratos tipados

El aprendizaje en contexto maximiza su efectividad cuando las demostraciones replican con total fidelidad los contratos de datos que gobernarán la entrada y la salida del modelo en producción. Cada elemento demostrativo formaliza un registro de entrada (`InteraccionValidada`: identificador, autor, canal, texto normalizado, metadatos de origen) y proyecta la respuesta correspondiente sobre el esquema analítico objetivo (`MensajeAnalizado`): vínculo invariable al `source_id`, asignación categórica de `sentimiento`, rama de `categoria_enrutamiento`, regla documentada que sustenta la decisión, listas explícitas de temas y entidades, y la descomposición entera de `PuntuacionRelevancia` acompañada de su justificación cualitativa en `motivo_seleccion`.

---

## 4. Mitigación de sesgos atencionales, orden demostrativo y degradación posicional

El incremento del número de ejemplos dentro de secuencias largas introduce anomalías atencionales inherentes a la arquitectura de autoatención autorregresiva. La literatura sobre transformadores describe una degradación de la atención en los segmentos intermedios de secuencias extensas ("*lost in the middle*"), donde la información situada en el núcleo de la ventana recibe menos ponderación que los elementos ubicados en los extremos.

Para contrarrestar esta atenuación, la ingeniería de contexto distribuye los elementos estratégicos aprovechando los efectos de primacía y recencia:

- Las instrucciones maestras del sistema y los contratos canónicos se anclan al inicio absoluto de la secuencia.
- Los casos límite más complejos y las distinciones sutiles entre clases se sitúan en las posiciones iniciales del bloque demostrativo o directamente en las posiciones finales que preceden al lote de evaluación.
- Se incorpora un reanclaje explícito de instrucciones inmediatamente después del catálogo de ejemplos, reiterando las directivas formales de clasificación antes de introducir los registros que deben ser procesados.
- El orden relativo en que se disponen las demostraciones no debe agrupar todos los ejemplos de una misma categoría de forma consecutiva: eso induce un sesgo de recencia artificial que sobrerrepresenta la última clase observada. La estructura del catálogo debe emplear un ordenamiento estratificado e intercalado que alterne de forma periódica entre casos de logros, dudas y dificultades.
- En las etapas de calibración semántica, es indispensable contrastar la estabilidad del agente ejecutando variantes con permutaciones controladas del catálogo, validando que el grado de concordancia en la asignación categórica permanezca robusto frente al orden de las demostraciones.

---

## 5. Dimensionamiento del catálogo `CATALOGO_FEW_SHOT`

### 5.1 Distribución recomendada

El catálogo debe equilibrarse cubriendo las combinaciones cruzadas entre `categoria_enrutamiento` (3 clases), `sentimiento` (4 clases) y la escala de `PuntuacionRelevancia` (0 a 6 puntos):

| Categoría de enrutamiento | Ejemplos necesarios | Variantes semánticas y casos específicos que deben cubrirse | Sentimiento asociado | Rango de relevancia |
| --- | --- | --- | --- | --- |
| **`Logro`** | 4 a 5 | Hito laboral verificado (empresa/puesto concreto, `apto_para_publicacion = True`) · Hito de aprendizaje con proyecto finalizado · Logro menor/vago sin datos verificables (`apto_para_publicacion = False`) · Caso mixto: logro tras superar frustraciones previas | `"Altamente Positivo"` o `"Positivo"` | 4 a 6 puntos |
| **`Duda`** | 4 a 5 | Consulta técnica profunda de alto valor comunitario · Duda técnica recurrente (error común de configuración/API) · Duda con tono entusiasta (evitar que se clasifique como logro) · Duda ambigua/incompleta (`utilidad_comunitaria = 0`) | `"Neutro"` o `"Positivo"` | 2 a 4 puntos |
| **`Dificultad`** | **5 (fijo, no reducible)** | Bloqueo pedagógico crítico (`requiere_soporte = True`) · Problema de plataforma o accesos · Frustración expresada cordialmente (evitar clasificar como neutro) · Dificultad técnica persistente con múltiples intentos fallidos · Dificultad con agradecimiento mezclado (caso límite sentimiento/enrutamiento) | `"Dificultad/Negativo"` | 2 a 4 puntos |
| **Casos límite / centinela** | 2 | Intento de *prompt injection* (demostrar que se analiza como texto plano) · Ruido irrelevante o saludo aislado (`puntuacion_relevancia.total = 0`, `apto_para_publicacion = False`) | `"Neutro"` | 0 a 1 puntos |

**Total: 15 a 17 ejemplos** (mínimo $4+4+5+2=15$; máximo $5+5+5+2=17$).

`Dificultad` se fija en 5 y queda protegida de cualquier recorte: es la única categoría que activa `requiere_soporte = True`, y de las tres es la que más cuesta fallar en detectar dado el riesgo de dejar a un estudiante sin canalizar a soporte. Si el presupuesto de tokens obliga a recortar el catálogo, la reducción debe salir de `Logro` o `Duda` (bajando a 4 cada una) antes que de `Dificultad`.

### 5.2 Presupuesto de tokens y activación de *prompt caching*

Cada ejemplo formateado bajo el esquema de entrada/salida consume en promedio entre 90 y 120 tokens. Un catálogo de 15 a 17 ejemplos representa aproximadamente **1,350 a 2,040 tokens**; sumando las instrucciones maestras y la taxonomía del agente (~400 tokens), el prefijo estático total alcanza **≈1,750 a 2,440 tokens**.

Este volumen supera con margen el umbral mínimo de activación de caché de OpenAI y Anthropic (1,024 tokens en ambos). Para Gemini, cuyo umbral puede llegar hasta **2,048 tokens según modo y región**, el extremo inferior del rango (≈1,750 tokens totales con 15 ejemplos cortos) **no garantiza** superar ese techo en la configuración más exigente del proveedor. Mitigación: medir el conteo real de tokens con el tokenizador de cada proveedor antes de fijar la constante — no asumir el promedio de 90–120 tokens sin verificarlo contra el texto final — y, si se necesita garantizar la activación bajo la configuración más estricta de Gemini, operar hacia el extremo superior del rango (17 ejemplos).

### 5.3 Control del sesgo de frecuencia (*prior bias*)

En Discord y foros de programación, el 70% o más de los mensajes son consultas técnicas rutinarias. Forzar en `CATALOGO_FEW_SHOT` un balance cercano a 1:1:1 entre `Logro`, `Duda` y `Dificultad` (manteniendo `Dificultad` siempre en 5) contrarresta el sesgo del modelo preentrenado de clasificar todo mensaje técnico como neutral, garantizando la detección efectiva de dificultades que requieren soporte.

---

## 6. Arquitectura de inyección de catálogos y gobernanza de caché de prefijo

### 6.1 El ensamblaje contextual en dos pasadas

Para capitalizar las ventajas computacionales de la caché de contexto, el sistema implementa una canalización de ensamblaje desacoplada en dos etapas. La primera pasada construye la capa estática del contexto — instrucciones de gobernanza del agente, definiciones taxonómicas y el catálogo demostrativo — y la serializa de manera idéntica al inicio absoluto del *prompt*. Al mantener este prefijo invariable en su secuencia exacta de bytes, los proveedores de inferencia reutilizan el estado computado de los tensores de clave y valor en memoria, eliminando la necesidad de recomputar la atención sobre los ejemplos. La segunda pasada concatena la capa dinámica efímera — el lote de mensajes comunitarios que requieren evaluación inmediata —, condicionando la generación sobre el catálogo ya procesado en memoria.

**Regla de oro de prefijo inmutable:** en los tres proveedores, la condición necesaria para el acierto de caché es que los primeros tokens del *prompt* sean idénticos byte a byte entre invocaciones. No deben inyectarse marcas de tiempo dinámicas, identificadores aleatorios ni datos del usuario dentro de la sección del catálogo de ejemplos.

### 6.2 Decisión de proveedores y rol de cada uno

Conforme a la directiva M10 (*"no fijar modelo por nombre histórico ni asumir cuotas gratuitas; probar opciones accesibles con los mismos lotes y elegir por fidelidad, formato, latencia y consumo"*), el equipo adoptó una estrategia híbrida multi-proveedor con conmutación en caliente:

| Rol | Proveedor / modelo | Justificación |
| --- | --- | --- |
| **Motor principal (producción MVP)** | OpenAI `gpt-4o-mini` | *Structured Outputs* nativos (`strict=True`): 0% de excepciones de parseo contra el esquema `MensajeAnalizado`. Latencia media 0.6–0.9 s. |
| **Motor secundario (calibración y pruebas masivas)** | Google Gemini `gemini-1.5-flash` | Ventana de contexto masiva, útil para pruebas de catálogos extensos sin degradar atención; ya preconfigurado en la VM de pruebas del equipo. |
| **Motor de contingencia (fallback)** | Anthropic `claude-3-5-haiku-20241022` | Alta disciplina de *tool calling* estructurado; activado ante agotamiento de cuota o caída de los dos proveedores anteriores. |

**Patrón de integración:** un proveedor desacoplado (*Model Factory*) en `ai_modules/multiprovider_agent.py`, gestionado con PydanticAI, que permite conmutar entre los tres proveedores mediante la variable `LLM_PROVIDER` del entorno sin alterar el código de negocio.

### 6.3 Divergencia en los paradigmas de caché por proveedor

Implementar conmutación en caliente entre proveedores introduce un reto que la decisión de proveedores por sí sola no resuelve: la caché de claves y valores (KV-Cache) reside físicamente en la memoria de los aceleradores del proveedor correspondiente, por lo que **el caché no es transferible entre modelos**. Cada proveedor aborda el *prompt caching* con una abstracción distinta:

| Criterio | OpenAI (`gpt-4o-mini`) | Anthropic (`claude-3-5-haiku`) | Google Gemini (`gemini-1.5-flash`) |
| --- | --- | --- | --- |
| **Mecanismo** | Automático / implícito (coincidencia exacta de prefijo) | Explícito por marcadores (`cache_control` en bloques) | Híbrido (implícito, o explícito vía `CachedContent`) |
| **Punto de activación** | Detección interna al enviar solicitudes con el mismo prefijo | Inyección de `{"type": "ephemeral"}` en el último bloque estático | Creación previa de un recurso de caché vía API, o prefijo común en modo implícito |
| **Umbral mínimo** | 1,024 tokens | 1,024 tokens | 1,024 a 2,048 tokens (según modo y región) |
| **Ciclo de vida / TTL** | Efímero (~5–10 min de inactividad) | 5 minutos por defecto (renovable) o 1 hora | Configurable (1 min a horas/días) en modo explícito |
| **Costos asociados** | Sin costo de escritura; ~50% de descuento en lectura | +25% de recargo al escribir; ~90% de descuento en lectura | Costo de almacenamiento por hora; ~90% de descuento en lectura (modo explícito) |

**Implicaciones técnicas adicionales:**

- **Asimetría de estado:** OpenAI y Claude operan sin estado — la petición HTTP contiene todo lo necesario. Gemini en modo explícito es *con estado*: exige crear y persistir un `cache_id`. Para no romper el diseño *stateless* que el resto del pipeline exige del agente (sección 6.1 del informe de selección de LLM: "el agente no requiere memoria permanente"), este proyecto usa **Gemini en modo implícito**, aun a costa de que su acierto de caché sea *best-effort* (sin garantía contractual de ahorro), a diferencia de OpenAI y Claude.
- **Penalización de arranque en frío:** al conmutar en caliente (p. ej., 429 en OpenAI → Claude o Gemini), el nuevo proveedor sufre 100% de *cache miss* en su primera llamada, con pico de latencia (TTFT) y de costo. En sistemas de alta concurrencia que alternan tráfico habitualmente, se requiere una rutina de *pre-calentamiento* (*cache warming*) que envíe un ping periódico al proveedor secundario para mantener caliente su KV-Cache.
- **Tokenizadores heterogéneos:** el mismo catálogo de ejemplos produce conteos de tokens distintos según el tokenizador de cada proveedor (variaciones de hasta ±10% observadas entre `cl100k_base`/`o200k_base`, el tokenizador de Anthropic y el de Gemini). Si para un proveedor el texto queda por debajo del umbral mínimo, el caché **falla silenciosamente**, cobrando la tarifa completa sin emitir errores explícitos. Por esto la sección 5.2 exige verificar el conteo real por tokenizador, no solo el promedio estimado.
- **Adaptador de formateo de *prompts*:** no es posible pasar una estructura de *prompt* idéntica a los tres SDK si se desea aprovechar el caché de Claude y Gemini. Un componente central (`CognitiveAgentFactory`, sección 6.4) actúa como fábrica/adaptador: para OpenAI concatena las directivas y el catálogo al inicio del rol *system* como texto continuo e inmutable; para Anthropic inserta el catálogo como bloque del sistema marcado con `cache_control`; para Gemini coloca el bloque estático estrictamente al inicio (modo implícito).
- **Normalización de telemetría:** los metadatos de tokens cacheados difieren por proveedor (`response.usage.prompt_tokens_details.cached_tokens` en OpenAI; `response.usage.cache_read_input_tokens` en Anthropic; `response.usage_metadata.cached_content_token_count` en Gemini). El módulo normaliza estos valores a un campo común (`tokens_cacheados: int`) para alimentar `structlog` y la clase `MetadatosEjecucion`.

### 6.4 Implementación de referencia: `CognitiveAgentFactory`

```python
"""Fábrica multi-proveedor de agentes cognitivos (ai_modules/multiprovider_agent.py)."""

from __future__ import annotations
from typing import Any
import structlog
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.models.openai import OpenAIModel

logger = structlog.get_logger("ai_modules.cache_orchestrator")
ANTHROPIC_CACHE_TTL = "5m"


class ProveedorNoSoportadoError(ValueError):
    """El valor de LLM_PROVIDER no corresponde a ningún proveedor implementado."""


class CognitiveAgentFactory:
    """Construye el Agent ya configurado con la estrategia de caché de su proveedor."""

    @staticmethod
    def create_agent(provider: str, static_catalog_prompt: str, result_schema: Any) -> Agent:
        provider = provider.lower()

        if provider == "openai":
            # Caching implícito: basta con que el system_prompt sea idéntico entre llamadas.
            model = OpenAIModel("gpt-4o-mini")
            return Agent(model=model, result_type=result_schema, system_prompt=static_catalog_prompt)

        if provider == "anthropic":
            # Caching declarativo: model_settings se aplica a nivel de constructor,
            # por lo que se activa automáticamente en cada run() sin repetirlo.
            model = AnthropicModel("claude-3-5-haiku-20241022")
            settings: AnthropicModelSettings = {"anthropic_cache_instructions": ANTHROPIC_CACHE_TTL}
            return Agent(
                model=model, result_type=result_schema,
                system_prompt=static_catalog_prompt, model_settings=settings,
            )

        if provider == "gemini":
            # Caching implícito de prefijo: se evita CachedContent explícito
            # para no introducir estado (cache_id) en un agente sin memoria.
            model = GeminiModel("gemini-1.5-flash")
            return Agent(model=model, result_type=result_schema, system_prompt=static_catalog_prompt)

        raise ProveedorNoSoportadoError(f"Proveedor LLM no soportado: {provider!r}")

    @staticmethod
    def extract_cached_tokens(provider: str, raw_usage: Any) -> int:
        """Normaliza la telemetría de tokens cacheados entre los tres proveedores."""
        provider = provider.lower()
        try:
            if provider == "openai":
                details = getattr(raw_usage, "prompt_tokens_details", None)
                return getattr(details, "cached_tokens", 0) if details is not None else 0
            if provider == "anthropic":
                return getattr(raw_usage, "cache_read_input_tokens", 0) or 0
            if provider == "gemini":
                return getattr(raw_usage, "cached_content_token_count", 0) or 0
        except Exception:
            return 0
        return 0
```

La clave `anthropic_cache_instructions` está verificada contra la documentación oficial de PydanticAI (acepta `True`, `'5m'` o `'1h'`) y se pasa como `model_settings` del constructor de `Agent` — no como un valor suelto — para que se aplique automáticamente a cada ejecución sin depender de que el código que invoca al agente la repita.

---

## 7. Resiliencia operativa y observabilidad

El pipeline incorpora defensas activas para asegurar una ejecución continua frente a contingencias de red o límites de cuota:

1. **Reintentos con *backoff* y conmutación en fallo:** el cliente ejecuta hasta 3 reintentos con retroceso exponencial y variación aleatoria (*jitter*) ante errores HTTP 429 o 5xx. Si el proveedor principal agota sus reintentos, el orquestador conmuta automáticamente al proveedor secundario configurado (ver sección 6.3 sobre la penalización de arranque en frío que esto implica).
2. **Degradación elegante:** si todos los proveedores fallan para un mensaje particular, el sistema no aborta el lote. Asigna `puntuacion_relevancia.total = 0`, `apto_para_publicacion = False`, preserva de forma obligatoria el `source_id` original y documenta en `motivo_seleccion`: *"Error de conexión con proveedor LLM; mensaje derivado a auditoría"*.
3. **Observabilidad estructurada con `structlog`:** cada inferencia emite un evento JSON con `run_id`, `request_id`, `proveedor`, `modelo`, `latencia_ms`, `prompt_tokens`, `completion_tokens`, `tokens_totales` y `tokens_cacheados` (normalizado según la sección 6.3), alimentando la clase `MetadatosEjecucion` del Contrato 3.
4. **Aislamiento de IDs operativos:** el LLM no genera `run_id` ni `request_id`; el *wrapper* en Python los inyecta al instanciar `AnalisisCognitivo`, evitando que el modelo asuma responsabilidades que puedan inducir alucinaciones de identificadores.
5. **Determinismo aritmético:** la suma de las tres subdimensiones de `PuntuacionRelevancia` (0 a 6) se calcula de forma determinista mediante un validador del modelo de datos (`@model_validator(mode="after")`), nunca confiando en la aritmética del LLM.

---

## 8. Metodología de validación empírica de la calibración

Ninguna de las secciones anteriores demuestra, por sí sola, que el catálogo calibra correctamente al agente — solo lo hacen plausible por diseño. La demostración exige un marco formal de evaluación cuantitativa sobre un conjunto reservado, con métricas por clase y umbrales de aceptación predefinidos.

### 8.1 Conjunto de validación reservado (*hold-out set*)

- **Aislamiento estricto de contaminación:** ningún mensaje del conjunto de validación puede formar parte de `CATALOGO_FEW_SHOT`.
- **Estratificación representativa:** entre 50 y 100 mensajes reales y anonimizados de Discord y foros, distribuidos entre las 4 clases de `sentimiento`, las 3 de `categoria_enrutamiento`, y con casos límite obligatorios (mensajes ambiguos, quejas mezcladas con agradecimientos, puntuaciones de relevancia extremas).
- **Etiquetado de referencia con doble ciego:** etiquetas establecidas manualmente por al menos dos revisores del equipo, con discrepancias resueltas por consenso.

### 8.2 Protocolo de prueba comparativa (A/B)

La calibración se considera exitosa solo si se cumplen **ambas** condiciones sobre el mismo *hold-out*:

1. **Condición A (línea base, *zero-shot*):** el modelo evalúa el conjunto únicamente con las directivas del sistema y el esquema Pydantic, sin `CATALOGO_FEW_SHOT`.
2. **Condición B (*few-shot*, catálogo calibrado):** el modelo evalúa el mismo conjunto incorporando el catálogo en su prefijo.
3. La Condición B debe superar a la Condición A de forma **estadísticamente significativa**, mediante la **prueba exacta de McNemar** sobre los pares discordantes de aciertos/errores entre ambas condiciones (muestras pareadas sobre el mismo *hold-out*, no una comparación de medias). Sea $b$ el número de mensajes que A clasifica correctamente y B falla, y $c$ el número de mensajes que A falla y B acierta: se rechaza la hipótesis nula $P(b)=P(c)$ a favor de B cuando el valor-p de la prueba binomial unilateral sobre $c$ de $n=b+c$ ensayos es menor que $\alpha = 0.05$. Se aplica por separado a `sentimiento` y a `categoria_enrutamiento`.
4. **Y además**, la Condición B debe alcanzar los umbrales mínimos de la sección 8.4.

### 8.3 Métricas de evaluación

**Variables categóricas** (`sentimiento`, `categoria_enrutamiento`) — la exactitud global (*accuracy*) esconde fallos en clases minoritarias críticas (`"Dificultad/Negativo"`), así que se computan métricas por clase:

$$\text{Precision}_c = \frac{\text{TP}_c}{\text{TP}_c + \text{FP}_c} \qquad \text{Recall}_c = \frac{\text{TP}_c}{\text{TP}_c + \text{FN}_c} \qquad F_1^{(c)} = 2 \cdot \frac{\text{Precision}_c \cdot \text{Recall}_c}{\text{Precision}_c + \text{Recall}_c}$$

$$\text{Macro } F_1 = \frac{1}{K} \sum_{c=1}^K F_1^{(c)} \qquad \kappa = \frac{p_o - p_e}{1 - p_e}$$

donde $\kappa$ (Kappa de Cohen) mide la concordancia entre el LLM y las etiquetas humanas penalizando los aciertos por azar.

**Puntuación de relevancia** (`PuntuacionRelevancia`, escala 0–6):

$$\text{MAE} = \frac{1}{N} \sum_{i=1}^N \vert y_i - \hat{y}_i \vert \qquad r_s \text{ (correlación de Spearman)}$$

### 8.4 Matriz de umbrales de aceptación para el MVP

| Dimensión evaluada | Métrica objetivo | Umbral mínimo | Justificación |
| --- | --- | --- | --- |
| Calibración vs. línea base | McNemar (p-valor, unilateral) | $< 0.05$ | La mejora proviene del catálogo, no del azar. |
| Enrutamiento editorial | Kappa de Cohen | $\ge 0.80$ | Concordancia "casi perfecta" con el criterio del equipo. |
| Enrutamiento editorial | $F_1$ por clase | $\ge 0.80$ en cada clase | Evita omitir dificultades o clasificar falsos logros. |
| Análisis de sentimiento | *Accuracy* global | $\ge 85.0\%$ | Umbral oficial de la tarea S1-1 / Requisito R2. |
| Análisis de sentimiento | Macro $F_1$ | $\ge 0.82$ | Protege clases minoritarias. |
| Relevancia (0–6) | MAE | $\le 0.75$ puntos | Error promedio bajo en la escala global. |
| Relevancia (0–6) | Spearman $r_s$ | $> 0$ | Orden correcto para el resumen semanal y LinkedIn. |
| Trazabilidad | Coincidencia de `source_id` | $100\%$ | Ningún identificador del lote puede faltar en la salida. |
| Integridad | Longitud `motivo_seleccion` | $0\%$ violaciones ($\ge 10$ caracteres) | Obliga a documentar evidencia observable. |
| Conformidad Pydantic | Excepciones de esquema | $0\%$ | `result_type` de PydanticAI lo garantiza antes de llegar aquí. |

### 8.5 Arnés de evaluación (`pytest`)

Implementado en `tests/test_calibration_eval.py`, marcado `@pytest.mark.integration` (se omite sin `OPENAI_API_KEY`, igual que `tests/test_oci_client.py`):

```python
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="requiere credenciales"),
]

def test_catalogo_supera_linea_base_y_umbrales_de_aceptacion(datos_validacion, interacciones_entrada):
    agente_zero_shot = CognitiveAgentFactory.create_agent(
        "openai", INSTRUCCIONES_BASE, AnalisisCognitivoPayload
    )
    pred_a = _ejecutar_lote(agente_zero_shot, interacciones_entrada)           # Condición A

    agente_few_shot = CognitiveAgentFactory.create_agent(
        "openai", INSTRUCCIONES_BASE + CATALOGO_FEW_SHOT, AnalisisCognitivoPayload
    )
    pred_b = _ejecutar_lote(agente_few_shot, interacciones_entrada)           # Condición B

    # pred_a / pred_b se indexan por source_id, nunca por posición de lista:
    # un LLM procesando un lote no garantiza preservar el orden de entrada.

    for tarea in ("sentimiento", "enrutamiento"):
        b, c, p_valor = _mcnemar_p_valor(acierto_a[tarea], acierto_b[tarea])
        assert p_valor < 0.05                                                  # sección 8.2

    assert reporte_sent["accuracy"] >= 0.85
    assert reporte_sent["macro avg"]["f1-score"] >= 0.82
    assert kappa_ruta >= 0.80
    for clase in clases_enrutamiento:
        assert reporte_ruta[clase]["f1-score"] >= 0.80
    assert mae_rel <= 0.75
    assert rho_rel > 0                                                         # matriz completa, sección 8.4
```

Puntos técnicos ya verificados en esta implementación: el resultado de `agent.run_sync(...)` se lee con `.output` (no `.data`, eliminado en PydanticAI ≥0.6.0); el agente se construye con `CognitiveAgentFactory.create_agent(...)`; y las predicciones se emparejan con la referencia por `source_id`, nunca por posición en la lista.

Si `uv run pytest -m integration tests/test_calibration_eval.py` pasa, queda demostrado que el catálogo (a) mejora de forma estadísticamente significativa sobre no usar ejemplos, y (b) cumple, en producción, la totalidad de los umbrales de calidad del MVP.

---

## 9. Síntesis de reglas operativas para el equipo

1. **Prefijo inmutable:** nada dinámico (timestamps, IDs aleatorios, datos de usuario) dentro del bloque de catálogo — debe ser idéntico byte a byte entre invocaciones para que los tres proveedores puedan acertar en caché.
2. **Modo *stateless* por defecto:** caché implícito en OpenAI y Gemini, marcado declarativo (`anthropic_cache_instructions`) en Claude — se evita programar y rastrear `cache_id` de Gemini en la máquina virtual.
3. **`Dificultad` nunca se recorta:** ante presión de presupuesto de tokens, se reduce `Logro`/`Duda` antes que la categoría que activa soporte a estudiantes.
4. **El tamaño del catálogo es un punto de partida, no un resultado:** 15–17 ejemplos está justificado por presupuesto de tokens y tipo de tarea, pero solo la sección 8 (McNemar + matriz de umbrales) decide si es suficiente. Si no se alcanzan los umbrales, el primer ajuste es mover la distribución al extremo superior (17) antes de escalar hacia regímenes *many-shot* de decenas adicionales.
5. **El agente nunca genera sus propios identificadores operativos** (`run_id`, `request_id`) ni suma a mano `PuntuacionRelevancia` — ambos se resuelven en la capa de software, no en el LLM.

---

## 10. Observaciones y recomendaciones para la implementación

Estas son las verificaciones y riesgos residuales que el equipo debería resolver antes de tratar esta metodología como cerrada:

1. **Vigencia de los identificadores de modelo.** `gemini-1.5-flash` y `claude-3-5-haiku-20241022` son los modelos con los que el equipo evaluó la arquitectura, pero la propia directiva M10 advierte explícitamente contra "fijar modelo por nombre histórico". Dado que este documento se redacta en octubre de 2026, conviene revalidar antes del Demo Day si esos *snapshots* siguen siendo las versiones recomendadas de cada proveedor, o si ya existen sucesores que el equipo debería evaluar con el mismo protocolo A/B de la sección 8.
2. **Separabilidad de `ai_modules/prompts/analyzer.py`.** Toda la metodología de la sección 8 (Condición A sin catálogo vs. Condición B con catálogo) asume que las instrucciones base y `CATALOGO_FEW_SHOT` existen como dos constantes separables en ese módulo. Si la Fase 2 del plan de trabajo las entregó como un solo bloque fusionado, hay que dividirlas antes de poder ejecutar el arnés de evaluación.
3. **Dependencias de desarrollo pendientes de declarar.** `tests/test_calibration_eval.py` requiere `scikit-learn` y `scipy`, que no están en `[dependency-groups] dev` de `pyproject.toml` (solo figuran `pytest` y `ruff`). Agregarlas antes de que CI o cualquier integrante intente correr la suite.
4. **Margen de caché de Gemini no garantizado en el extremo inferior del catálogo.** Con 15 ejemplos cortos (~90 tokens cada uno), el prefijo total (~1,750 tokens) puede no alcanzar el techo de 2,048 tokens que Gemini exige en su configuración más estricta. Antes de fijar la constante final, medir el conteo real de tokens con el tokenizador de cada proveedor sobre el catálogo efectivamente escrito, no sobre el promedio estimado.
5. **Verificación de `AnthropicModelSettings` en tiempo de ejecución.** La clave `anthropic_cache_instructions` está confirmada contra la documentación y el changelog oficiales de PydanticAI, y el código de la sección 6.4 ya la conecta correctamente al constructor de `Agent` (defecto corregido respecto a una versión anterior del código, que calculaba el `dict` de ajustes y nunca lo pasaba al agente). Queda pendiente una prueba de humo contra el paquete real instalado en el entorno del equipo para confirmar que `agent.model_settings` se expone con ese nombre de atributo en la versión de PydanticAI que finalmente se fije en `uv.lock`.
6. **Enmascaramiento de PII como mitigación, no como garantía.** La sanitización por regex/NER de la sección 3.1 no cubre con certeza apodos informales o menciones sin `@` propias de Discord. Conviene una revisión manual puntual del catálogo final — especialmente porque, una vez fijado como prefijo estático cacheado, cualquier corrección posterior invalida la caché acumulada y debe tratarse como un cambio de versión, no como un parche silencioso.
7. **Costo de las pruebas de calibración.** El arnés de la sección 8.5 ejecuta el modelo real dos veces (Condición A y B) sobre 50–100 mensajes cada vez; al estar marcado `@pytest.mark.integration` se excluye de la ejecución por defecto (`addopts = "-m 'not integration'"` en `pyproject.toml`), pero el equipo debe presupuestar el costo y la latencia de correrlo explícitamente antes de cada ajuste al catálogo, y no solo antes del Demo Day.
8. **Repetir la Fase 4 del plan con los otros dos proveedores.** La sección 8 valida explícitamente solo el proveedor `"openai"`. El plan de trabajo original (Fase 4: "Calibración Cruzada") previó contrastar métricas entre `gpt-4o-mini` y `gemini-1.5-flash`; el mismo arnés de `pytest` debería parametrizarse por proveedor (`pytest.mark.parametrize("provider", [...])`) para no dejar sin validar empíricamente los modos de contingencia y calibración.

---

## Obras citadas

1. Effective context engineering for AI agents — Anthropic, <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
2. Exploring Effective Context Engineering for AI Agents — Jean Labelle, <https://jeanlabelle.ca/exploring-effective-context-engineering-for-ai-agents-lessons-from-anthropic/>
3. Context Engineering: The 2026 Playbook for AI Agents — Crux Digits, <https://cruxdigits.nl/blog/context-engineering-ai-agents-2026/>
4. Effective Context Engineering for AI Agents: A Developer's Guide, <https://machinelearningmastery.com/effective-context-engineering-for-ai-agents-a-developers-guide/>
5. Many-Shot In-Context Learning, <https://proceedings.neurips.cc/paper_files/paper/2024/file/8cb564df771e9eacbfe9d72bd46a24a9-Paper-Conference.pdf>
6. [LCFM workshop @ICML 2024] Many-Shot In-Context Learning, <https://neurips.cc/media/neurips-2024/Slides/96277_u7Fu4aS.pdf>
7. [2404.11018] Many-Shot In-Context Learning — arXiv, <https://arxiv.org/abs/2404.11018>
8. Scaling Many-shot In-context Learning via Continued Pretraining, <https://arxiv.org/pdf/2509.06806>
9. FROM FEW TO MANY: SELF-IMPROVING MANY — OpenReview, <https://openreview.net/pdf?id=JBXO05r4AV>
10. Prompt Caching Infrastructure: Reducing LLM Costs and Latency, <https://introl.com/blog/prompt-caching-infrastructure-llm-cost-latency-reduction-guide-2025>
11. Prompt Caching Explained: How to Cut AI Latency and Cost Without Changing Your Model, <https://blog.gopenai.com/prompt-caching-explained-how-to-cut-ai-latency-and-cost-without-changing-your-model-1f8930b79480>
12. Context caching — Interactions API — Google AI for Developers, <https://ai.dev/gemini-api/docs/caching>
13. Context caching overview — Gemini Enterprise Agent Platform, <https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview>
14. Vertex AI context caching — Google Cloud Blog, <https://cloud.google.com/blog/products/ai-machine-learning/vertex-ai-context-caching>
15. AnthropicModelSettings — PydanticAI docs, <https://pydantic.dev/docs/ai/models/anthropic/>
16. Agent API reference — PydanticAI docs, <https://pydantic.dev/docs/ai/api/pydantic-ai/agent/>
17. PydanticAI Changelog, <https://pydantic.dev/docs/ai/project/changelog/index.md>
18. prompt - How should examples be formatted for one-shot or few-shot — Stack Overflow, <https://stackoverflow.com/questions/79268425/how-should-examples-be-formatted-for-one-shot-or-few-shot-prompting-when-using-o>
19. Optimizing Large Language Models using Multi-Shot Learning, <https://medium.com/@zbabar/optimizing-large-language-models-using-multi-shot-learning-9ee9eb98709b>
20. Plan_Agente_Analisis_Cognitivo.md (documento interno del proyecto)
21. Guia_Reproducibilidad_CommunityLab.pdf (documento interno del proyecto)
22. esquemas_diff.txt / esquemas_propuesto.py (contratos internos del proyecto)
23. CommunityLab_Integracion_Michael_Andrea_Roberto_Primera_Prueba_v2.pdf (documento interno del proyecto)
24. AGENTS_FT.md (documento interno del proyecto)
