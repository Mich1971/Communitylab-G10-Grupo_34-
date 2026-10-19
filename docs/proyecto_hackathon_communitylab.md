# Proyecto 3 - Programa ONE (Grupo 10)
## CommunityLab – Motor Inteligente de Transformación y Distribución para Comunidades Digitales

---

### Mapeo de Formaciones ONE (Grupo 10)

* **Formaciones Indispensables:** Desarrollo y Orquestación con IA Generativa, Ingeniería de Agentes y Automatización con IA y Oracle Cloud Infrastructure (OCI).
* **Formaciones de Refuerzo:** Inteligencia de Datos y RAG Avanzado.
* **Núcleo Técnico:** Ingeniería de prompts y copywriting persuasivo para múltiples canales, automatización de flujos de curaduría con nodos condicionales y persistencia de activos en OCI Object Storage en la capa *Always Free*.

---

### Sector empresarial
**MarTech / Gestión de Comunidades Digitales / Marketing de Contenidos & Engagement**

Soluciones inteligentes orientadas a comunidades de aprendizaje, ecosistemas de desarrolladores, empresas SaaS y creadores de contenido. El sector necesita monitorear grandes volúmenes de conversaciones orgánicas, dudas, retroalimentación, entregas de proyectos y testimonios dispersos en canales digitales (Discord, Slack, Foros, GitHub, Formularios) y transformarlos de manera sistemática en activos de marketing, crecimiento y retención.

---

### Descripción del proyecto

Desarrollar una solución inteligente y automatizada capaz de ingerir la actividad orgánica de una comunidad digital (mensajes, debates en foros, feedback de cursos, entregas de proyectos de estudiantes, interacciones en chats), analizar estos datos y convertirlos automáticamente en activos de distribución listos para su publicación y toma de decisiones.

Hoy en día, las comunidades activas generan a diario decenas de historias inspiradoras, dudas recurrentes y testimonios valiosos, pero la mayor parte de ese valor se pierde en el historial de los canales porque la curaduría y la producción manual de contenidos de marketing consumen muchas horas de los equipos de Community Management y Marketing.

CommunityLab permite que el equipo elija y construya una solución enfocada dentro de este problema, como por ejemplo:

* **Generador Automático de Contenido para Redes Sociales:** Creación de publicaciones estructuradas para LinkedIn, X (Twitter) y Newsletters a partir de debates y logros de la comunidad.
* **Detector Inteligente de Historias de Éxito y Testimonios:** Identificación de relatos de superación, contrataciones laborales y proyectos destacados con extracción de citas y casos listos para marketing.
* **Motor de FAQ Dinámico y Contenido Educativo:** Transformación de dudas frecuentes de los estudiantes en tutoriales rápidos, tips de la semana y temas de documentación.
* **Dashboard de Salud y Sentimiento de la Comunidad:** Análisis continuo del sentimiento de los miembros, detección de temas en tendencia y alertas sobre miembros que necesitan apoyo.

#### La inteligencia de la aplicación deberá:
1. **Ingerir** datos de interacciones en lote o en tiempo real (vía JSON, CSV, Webhook o integración con plataformas).
2. **Procesar** el contenido con LLMs a elección del equipo (Google Gemini, OpenAI ChatGPT, Anthropic Claude o equivalentes), analizando sentimiento, temas y relevancia comunicacional.
3. **Orquestar** workflows automatizados (utilizando n8n, LangChain/LangGraph o scripts de Python equivalentes) para redactar copys optimizados para diferentes canales de distribución.
4. **Almacenar** los activos generados e informes en OCI Object Storage (capa *Always Free*).

---

### Necesidad del cliente (explicación no técnica)

Los equipos de marketing y gestión de comunidades pasan horas navegando por mensajes dispersos en Discord o Slack intentando encontrar testimonios espontáneos de estudiantes o pensando qué publicar en las redes sociales de la institución.

La solución debe permitir:
* Capturar automáticamente lo que está sucediendo en la comunidad sin esfuerzo manual.
* Identificar a quienes lograron un hito importante o plantearon una duda enriquecedora de forma instantánea.
* Generar publicaciones persuasivas y listas para publicar en LinkedIn y Twitter, respetando la voz de la marca.
* Crear resúmenes semanales de la comunidad (*Community Highlights*) en segundos.
* Monitorear el sentimiento general, identificando si la comunidad está comprometida, satisfecha o con dificultades.

---

### Validación de mercado

El concepto de **Community-Led Growth** (Crecimiento Impulsado por Comunidades) y **Marketing de Contenidos guiado por IA** es una de las mayores tendencias en el ecosistema global de tecnología:

* Los contenidos auténticos generados por los propios usuarios (*User Generated Content* - UGC) poseen tasas de conversión y *engagement* hasta 4 veces mayores que la publicidad corporativa tradicional.
* La automatización de flujos con IA ($n8n + \text{LLMs}$) elimina el trabajo manual repetitivo de recolección y redacción, permitiendo que equipos reducidos publiquen contenidos de alta calidad diariamente.
* Las plataformas que monitorean el sentimiento y generan activos sistemáticos transforman la comunidad en un motor sostenible de adquisición de nuevos estudiantes y retención de talento.

---

### Objetivo del Hackathon

Desarrollar un MVP funcional de motor inteligente de contenido capaz de:

1. Procesar datos de actividades e interacciones de una comunidad digital (datos simulados o reales).
2. Realizar análisis de sentimiento, categorización de temas e identificación de historias/insights relevantes utilizando LLMs (Google Gemini, OpenAI, Claude u otros modelos).
3. Orquestar la generación automática de al menos dos formatos distintos de activos de marketing/distribución (ej.: Publicación para LinkedIn + Resumen Semanal de la Comunidad, o Caso de Testimonio + Tema de FAQ).
4. Implementar la automatización del flujo utilizando n8n (con nodos de IA) o pipelines en Python (LangChain / LangGraph) o equivalentes.
5. Disponer de una interfaz amigable (Streamlit / Gradio) o panel de curaduría/aprobación.
6. Integrar la solución obligatoriamente con OCI Object Storage (capa *Always Free*).

---

### Resultados esperados

#### 1. IA, Procesamiento de Texto & Generación de Copy
Notebook o módulos que contengan:
* Ingestión y limpieza de datos de conversaciones/feedback de la comunidad.
* Análisis de sentimiento y extracción de entidades/temas con LLMs.
* Cadenas de prompt engineering especializadas en copywriting para diferentes canales (LinkedIn, X, Newsletter).
* Mecanismo de puntuación de relevancia para seleccionar los mejores momentos de la comunidad.

#### 2. Automatización de Flujos & Interfaz
Workflow en n8n o aplicación Python que contenga:
* Flujo automatizado integrando disparadores (Webhook, Google Sheets, Formulario o Chat) con nodos de inteligencia artificial.
* Bifurcación de condiciones (ej.: si el sentimiento es sumamente positivo $\rightarrow$ generar Caso de Éxito; si es una duda recurrente $\rightarrow$ generar Tip/FAQ).
* Interfaz web interactiva (Streamlit o Gradio) para la visualización de los datos de la comunidad y aprobación de las publicaciones generadas.

#### 3. Oracle Cloud Infrastructure (OCI) - Capa Always Free
* **Requisito Obligatorio (MVP):** OCI Object Storage - Creación de Bucket *Always Free* para almacenar los paquetes de activos generados (textos formateados, informes y JSONs).
* **Recurso Opcional / Diferencial:** OCI Compute Instance ("Despliegue Completo en la Nube") - Alojamiento de la aplicación o contenedor n8n en una máquina virtual Linux *Always Free* en OCI.
* **Aviso Importante ONE:** Utilice exclusivamente recursos de la capa *Always Free* de OCI, manteniendo la conformidad con la gratuidad integral del programa social ONE.

#### 4. Documentación & Demostración
* Repositorio en GitHub con historial de commits organizado.
* `README.md` completo con el diagrama de arquitectura del pipeline y guía paso a paso de despliegue.
* Demostración práctica presentando el procesamiento de un lote de mensajes de la comunidad y la generación simultánea de los activos de distribución.

---

### Funcionalidades obligatorias (MVP)

#### Endpoint / Acción: Procesamiento de Actividad y Generación de Activos
El sistema debe recibir un conjunto de mensajes/interacciones de la comunidad y retornar el análisis consolidado acompañado de los activos de marketing listos para su uso.

##### Ejemplo de Solicitud (Entrada):
```json
{
  "origen_comunidad": "Discord_Grupo_ONE_G10",
  "periodo_referencia": "Semana_04",
  "interacciones": [
    {
      "autor": "Mariana Souza",
      "canal": "#logros-y-empleos",
      "tipo": "testimonio",
      "texto": "Comunidad, quede seleccionada para el puesto de Desarrolladora Junior de IA! El proyecto del curso de LangChain y OCI que construi en mi portfolio marco toda la diferencia en la entrevista tecnica. Muy agradecida con la comunidad por todo el apoyo!"
    },
    {
      "autor": "Lucas Albuquerque",
      "canal": "#dudas-langgraph",
      "tipo": "pregunta_tecnica",
      "texto": "Tengo dudas sobre como estructurar los nodos condicionales en LangGraph cuando la respuesta del LLM necesita reintento. Alguien tiene un ejemplo practico de router?"
    }
  ]
}
```

##### Ejemplo de Respuesta (Salida Estructurada):
```json
{
  "status": "exito",
  "resumen_comunidad": {
    "total_interacciones_procesadas": 2,
    "sentimiento_predominante": "Altamente Positivo",
    "temas_principales": [
      "Contratacion / Logros",
      "LangGraph / Nodos Condicionales"
    ]
  },
  "activos_distribucion_generados": {
    "post_linkedin": {
      "titulo": "De la Comunidad al Mercado: El impacto de los proyectos practicos de IA",
      "copy": "Nada nos da mas orgullo que ver a nuestros talentos conquistando el mercado de tecnologia!\n\nNuestra estudiante Mariana Souza acaba de ser contratada como Desarrolladora Junior de IA tras destacar sus proyectos practicos desarrollados con LangChain y Oracle Cloud Infrastructure.\n\nHistorias como la de Mariana demuestran que construir soluciones reales es el mejor camino para impulsar la carrera tech. Felicitaciones, Mariana!\n\n#TalentosTech #InteligenciaArtificial #OracleCloud #CarreraDev",
      "canal_recomendado": "LinkedIn Oficial",
      "potencial_engagement": "Alto"
    },
    "destaque_newsletter_semanal": {
      "seccion": "Logro de la Semana",
      "titular": "Estudiante consigue empleo dev con portfolio de IA en Oracle Cloud",
      "resumen": "Mariana Souza obtuvo su primera oportunidad como Dev Jr de IA destacando proyectos desarrollados durante la formacion."
    },
    "sugerencia_contenido_faq": {
      "tema": "Tip Rapido: Como crear nodos de reintento en LangGraph",
      "origen": "Duda frecuente planteada por Lucas Albuquerque en el canal de soporte",
      "status": "derivado_a_mentoria"
    }
  },
  "almacenamiento_oci": {
    "bucket": "communitylab-activos-marketing",
    "ruta_objeto": "activos/2026-semana-04/paquete-distribucion.json",
    "status": "guardado_con_exito"
  }
}
```

---

### Requisitos mínimos (Checklist de Evaluación)

- [ ] Ingestión funcional de interacciones de la comunidad (mensajes, CSV o JSON simulando canales).
- [ ] Análisis de sentimiento y extracción de temas utilizando LLMs (Google Gemini, OpenAI, Claude o equivalentes).
- [ ] Generación automatizada de al menos 2 formatos de activos de marketing (ej.: Post LinkedIn + Newsletter o FAQ + Caso de Éxito).
- [ ] Orquestación del flujo utilizando n8n, Python (LangChain / LangGraph) o herramientas equivalentes.
- [ ] Integración activa con OCI Object Storage (capa *Always Free*) para almacenar los paquetes de activos generados.
- [ ] Demostración de un mínimo de 3 ejemplos de transformación de interacciones en activos de publicación.
- [ ] Repositorio en GitHub con documentación y diagrama de la línea de distribución.

---

### Recursos opcionales (Diferenciales)

* **Despliegue Completo en la Nube (OCI Compute):** Alojamiento de la aplicación o contenedor n8n en una máquina virtual Linux *Always Free* en OCI.
* **Flujo Completo de Automatización en n8n:** Webhook que recibe mensajes directamente de formularios o Discord y genera borradores en Google Docs o envía correos electrónicos.
* **Bot Interactivo de Comunidad:** Asistente en Discord/Telegram que responde dudas técnicas y detecta testimonios automáticamente.
* **Panel de Curaduría en Streamlit:** Interfaz visual para que el equipo de marketing edite, apruebe y publique los posts generados con un clic.
* **Generación de Imágenes para Publicaciones:** Creación de tarjetas conmemorativas o banners para redes sociales mediante herramientas multimodales.

---

### Directrices técnicas para los estudiantes

#### IA Generativa & Ingeniería de Prompts
* **Modelos de Lenguaje:** Los equipos tienen total autonomía para elegir entre Google Gemini, OpenAI (ChatGPT / GPT-4o), Anthropic (Claude), Grok o modelos locales/open-source.
* **Ingeniería de Prompts:** Ajuste los system prompts para garantizar tonos de voz adecuados para cada canal (tono inspirador en LinkedIn, conciso en Twitter/X, didáctico en FAQ).
* **Few-Shot Learning:** Utilice ejemplos en el prompt para enseñar al modelo a replicar el estilo de comunicación deseado.

#### Automatización & Workflows
* **Tecnologías Sugeridas:** n8n para automatización de flujos con nodos de IA e integraciones con Slack/Gmail/Sheets, y Streamlit o Gradio para interfaces de control.
* **Flexibilidad:** Soluciones en Python puro, Node.js u otras plataformas de automatización (ej.: Make) son plenamente aceptadas.

#### Oracle Cloud Infrastructure (OCI)
* **Object Storage (Obligatorio):** Persista todos los paquetes de activos generados en un Bucket *Always Free*.
* **Atención a los Costos:** Mantenga el uso restringido a los recursos gratuitos de la capa *Always Free*.