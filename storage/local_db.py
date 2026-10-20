"""
Módulo de Persistencia Local (Data Engineering).
Gestión de SQLite con esqueleto de 19 tablas, optimizado para alta concurrencia.
"""

import os
import sqlite3
from datetime import datetime, timezone
import structlog
from typing import Any, Dict, List, Tuple

from esquemas import AnalisisCognitivo, InteraccionValidada

logger = structlog.get_logger("storage.local_db")
DB_PATH = "data/communitylab.db"
DB_TIMEOUT_SEGUNDOS = 30.0  # Evita excepciones de "database is locked" por concurrencia

# =============================================================================
# SCRIPT DDL OPTIMIZADO (Fase 1 - Flexibilizado para soporte de Lotes Vacíos)
# =============================================================================
DDL_ESQUELETO = """
-- 1. MAESTRO DE LOTES Y AUDITORÍA (Contrato 1)
CREATE TABLE IF NOT EXISTS lotes_procesamiento (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    origen_comunidad TEXT NOT NULL,
    periodo_referencia TEXT NOT NULL,
    total_recibidos INTEGER NOT NULL CHECK (total_recibidos >= 0),
    total_validos INTEGER NOT NULL CHECK (total_validos >= 0),
    total_rechazados INTEGER NOT NULL CHECK (total_rechazados >= 0),
    fecha_procesamiento TEXT NOT NULL
);

-- 2. TABLA NUCLEAR DE ANALÍTICA (Contrato 2)
CREATE TABLE IF NOT EXISTS mensajes_analizados (
    source_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    autor TEXT NOT NULL,
    canal TEXT NOT NULL,
    tipo TEXT NOT NULL,
    texto_limpio TEXT NOT NULL,
    sentimiento TEXT NOT NULL CHECK (sentimiento IN ('Altamente Positivo', 'Positivo', 'Neutro', 'Dificultad/Negativo')),
    categoria_enrutamiento TEXT NOT NULL CHECK (categoria_enrutamiento IN ('Logro', 'Duda', 'Dificultad')),
    regla_aplicada TEXT NOT NULL,
    evidencia_explicita INTEGER NOT NULL CHECK (evidencia_explicita BETWEEN 0 AND 2),
    utilidad_comunitaria INTEGER NOT NULL CHECK (utilidad_comunitaria BETWEEN 0 AND 2),
    claridad_contexto INTEGER NOT NULL CHECK (claridad_contexto BETWEEN 0 AND 2),
    puntuacion_total INTEGER NOT NULL CHECK (puntuacion_total BETWEEN 0 AND 6),
    motivo_seleccion TEXT NOT NULL CHECK (length(motivo_seleccion) >= 10),
    requiere_soporte INTEGER NOT NULL DEFAULT 0 CHECK (requiere_soporte IN (0, 1)),
    apto_para_publicacion INTEGER NOT NULL CHECK (apto_para_publicacion IN (0, 1)),
    fecha_analisis TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- 3-4. TEMAS Y ENTIDADES (Relacionales con borrado en cascada)
CREATE TABLE IF NOT EXISTS mensaje_temas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    tema TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mensaje_entidades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    entidad TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE
);

-- 5. SÍNTESIS AGREGADA POR LOTE (Contrato 2 - Flexibilizado a >= 0 para resiliencia)
CREATE TABLE IF NOT EXISTS resumen_comunidad (
    run_id TEXT PRIMARY KEY,
    total_interacciones_procesadas INTEGER NOT NULL CHECK (total_interacciones_procesadas >= 0),
    registros_validos INTEGER NOT NULL CHECK (registros_validos >= 0),
    registros_rechazados INTEGER NOT NULL DEFAULT 0 CHECK (registros_rechazados >= 0),
    sentimiento_predominante TEXT NOT NULL,
    fecha_generacion TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resumen_distribucion_sentimiento (
    run_id TEXT NOT NULL,
    sentimiento TEXT NOT NULL,
    conteo INTEGER NOT NULL CHECK (conteo >= 0),
    PRIMARY KEY (run_id, sentimiento),
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resumen_temas_principales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tema TEXT NOT NULL,
    orden INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resumen_alertas_soporte (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    alerta TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- 6. FLUJO DE CURADURÍA Y PERSISTENCIA (Contrato 3)
CREATE TABLE IF NOT EXISTS paquetes_salida (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    fecha_generacion TEXT NOT NULL,
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS metadatos_ejecucion_paquete (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    modelo TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    latencia_ms INTEGER NOT NULL CHECK (latencia_ms >= 0),
    tokens_totales INTEGER NOT NULL CHECK (tokens_totales >= 0),
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id, numero_revision) REFERENCES paquetes_salida (run_id, numero_revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS post_linkedin (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    titulo TEXT NOT NULL CHECK (length(titulo) >= 5),
    canal_recomendado TEXT NOT NULL DEFAULT 'LinkedIn Oficial',
    cuerpo TEXT NOT NULL CHECK (length(cuerpo) >= 20),
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id, numero_revision) REFERENCES paquetes_salida (run_id, numero_revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS post_linkedin_hashtags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    hashtag TEXT NOT NULL,
    FOREIGN KEY (run_id, numero_revision) REFERENCES post_linkedin (run_id, numero_revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS post_linkedin_source_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    FOREIGN KEY (run_id, numero_revision) REFERENCES post_linkedin (run_id, numero_revision) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resumen_semanal (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    seccion TEXT NOT NULL,
    titular TEXT NOT NULL CHECK (length(titular) >= 5),
    resumen TEXT NOT NULL CHECK (length(resumen) >= 20),
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id, numero_revision) REFERENCES paquetes_salida (run_id, numero_revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS resumen_semanal_source_ids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    FOREIGN KEY (run_id, numero_revision) REFERENCES resumen_semanal (run_id, numero_revision) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES mensajes_analizados (source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS estado_revision (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aprobado', 'rechazado')),
    revisor TEXT,
    fecha_decision TEXT,
    comentarios TEXT,
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id, numero_revision) REFERENCES paquetes_salida (run_id, numero_revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidencia_almacenamiento_oci (
    run_id TEXT NOT NULL,
    numero_revision INTEGER NOT NULL,
    bucket TEXT NOT NULL DEFAULT 'communitylab-activos-marketing',
    ruta_objeto TEXT NOT NULL,
    status_almacenamiento TEXT NOT NULL DEFAULT 'no_iniciado' CHECK (status_almacenamiento IN ('guardado_con_exito', 'guardado_error', 'no_iniciado')),
    comprobacion_lectura INTEGER NOT NULL DEFAULT 0 CHECK (comprobacion_lectura IN (0, 1)),
    PRIMARY KEY (run_id, numero_revision),
    FOREIGN KEY (run_id, numero_revision) REFERENCES paquetes_salida (run_id, numero_revision) ON DELETE CASCADE
);

-- 7. TELEMETRÍA OPERATIVA
CREATE TABLE IF NOT EXISTS telemetria_llamadas_llm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    source_id TEXT,
    proveedor TEXT NOT NULL,
    modelo TEXT NOT NULL,
    latencia_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    tokens_totales INTEGER,
    tokens_cacheados INTEGER DEFAULT 0,
    fecha_evento TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- 8. AUDITORÍA DE RECHAZADOS (DLQ)
CREATE TABLE IF NOT EXISTS auditoria_rechazados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    request_id TEXT NOT NULL,
    mensaje_id TEXT,
    autor TEXT,
    canal TEXT,
    error_detalle TEXT NOT NULL,
    texto_crudo TEXT,
    fecha_isolation TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- ÍNDICES ESTRATÉGICOS DE CONSULTA
CREATE INDEX IF NOT EXISTS idx_analisis_run_id ON mensajes_analizados (run_id);
CREATE INDEX IF NOT EXISTS idx_analisis_sentimiento ON mensajes_analizados (sentimiento);
CREATE INDEX IF NOT EXISTS idx_analisis_enrutamiento ON mensajes_analizados (categoria_enrutamiento);
CREATE INDEX IF NOT EXISTS idx_analisis_puntuacion ON mensajes_analizados (puntuacion_total DESC);
CREATE INDEX IF NOT EXISTS idx_temas_source_id ON mensaje_temas (source_id);
CREATE INDEX IF NOT EXISTS idx_entidades_source_id ON mensaje_entidades (source_id);
CREATE INDEX IF NOT EXISTS idx_paquetes_revision ON paquetes_salida (run_id, numero_revision);

-- OPTIMIZACIÓN: Índice compuesto estratégico para acelerar el renderizado de la cola de curaduría de Sergio
CREATE INDEX IF NOT EXISTS idx_paquetes_revision ON paquetes_salida (run_id, numero_revision);
-- VISTAS ANALÍTICAS (Sin cambios, beneficiadas por los nuevos índices y cascadas)
CREATE VIEW IF NOT EXISTS vista_resumen_sentimiento AS
SELECT m.run_id, l.periodo_referencia, m.sentimiento, COUNT(m.source_id) AS total_mensajes, ROUND(AVG(m.puntuacion_total), 2) AS promedio_relevancia
FROM mensajes_analizados m JOIN lotes_procesamiento l ON m.run_id = l.run_id GROUP BY m.run_id, l.periodo_referencia, m.sentimiento;
CREATE VIEW IF NOT EXISTS vista_temas_tendencia AS
SELECT t.tema, COUNT(t.source_id) AS frecuencia, ROUND(AVG(m.puntuacion_total), 2) AS relevancia_media
FROM mensaje_temas t JOIN mensajes_analizados m ON t.source_id = m.source_id GROUP BY t.tema;
CREATE VIEW IF NOT EXISTS vista_alertas_soporte AS
SELECT source_id, autor, canal, texto_limpio, motivo_seleccion, fecha_analisis
FROM mensajes_analizados WHERE requiere_soporte = 1 OR sentimiento = 'Dificultad/Negativo';
CREATE VIEW IF NOT EXISTS vista_candidatos_publicacion AS
SELECT source_id, autor, canal, categoria_enrutamiento, puntuacion_total, motivo_seleccion, texto_limpio
FROM mensajes_analizados WHERE apto_para_publicacion = 1 AND categoria_enrutamiento = 'Logro' AND puntuacion_total >= 4;
CREATE VIEW IF NOT EXISTS vista_telemetria_lote AS
SELECT run_id, proveedor, modelo, COUNT(*) AS llamadas, ROUND(AVG(latencia_ms), 1) AS latencia_media_ms, SUM(tokens_totales) AS tokens_totales_lote, SUM(tokens_cacheados) AS tokens_cacheados_lote
FROM telemetria_llamadas_llm GROUP BY run_id, proveedor, modelo;
CREATE VIEW IF NOT EXISTS vista_cola_curaduria AS
SELECT p.run_id, p.numero_revision, p.fecha_generacion, pl.titulo AS titulo_linkedin, pl.cuerpo AS cuerpo_linkedin, rs.titular AS titular_resumen_semanal, rs.resumen AS resumen_semanal, er.estado, er.revisor, er.comentarios
FROM paquetes_salida p JOIN estado_revision er ON p.run_id = er.run_id AND p.numero_revision = er.numero_revision LEFT JOIN post_linkedin pl ON p.run_id = pl.run_id AND p.numero_revision = pl.numero_revision LEFT JOIN resumen_semanal rs ON p.run_id = rs.run_id AND p.numero_revision = rs.numero_revision WHERE er.estado = 'pendiente';
"""

def registrar_analisis_bloque(
    analisis_cognitivo: AnalisisCognitivo, 
    interacciones_origen: List[InteraccionValidada]
) -> Tuple[int, int]:
    """
    Inserta en bloque los resultados analíticos de la IA de forma idempotente y atómica.
    Aplica una purga preventiva total por run_id antes de escribir para tolerar reintentos.
    Retorna: Tuple[int, int] con (registros_sanos, registros_rechazados_dlq).
    """
    run_id = analisis_cognitivo.run_id
    request_id = analisis_cognitivo.request_id
    fecha_analisis = datetime.now(timezone.utc).isoformat()
    
    # Mapeo de interacciones de origen por ID para cruce de datos
    dict_origen = {int_val.id: int_val for int_val in interacciones_origen}
    
    tuplas_mensajes = []
    tuplas_temas = []
    tuplas_entidades = []
    tuplas_dlq = []

    for msg_ia in analisis_cognitivo.mensajes_analizados:
        source_id = msg_ia.source_id
        msg_origen = dict_origen.get(source_id)

        # DLQ Caso 1: ID Huérfano (No existe en los datos de la ingesta)
        if not msg_origen:
            logger.error("error_integridad_origen", source_id=source_id, run_id=run_id)
            tuplas_dlq.append((
                run_id, request_id, source_id, "Desconocido", "Desconocido",
                "ERROR_INTEGRIDAD: El ID analizado por la IA no existe en la ingesta original.",
                f"Motivo IA: {msg_ia.motivo_seleccion}", fecha_analisis
            ))
            continue

        # DLQ Caso 2: Violación de regla de negocio (Motivo de selección menor a 10 caracteres)
        if len(msg_ia.motivo_seleccion) < 10:
            logger.error("error_validacion_motivo_corto", source_id=source_id, run_id=run_id)
            tuplas_dlq.append((
                run_id, request_id, source_id, msg_origen.autor, msg_origen.canal,
                "ERROR_NEGOCIO_CHECK: 'motivo_seleccion' inferior a 10 caracteres.",
                msg_origen.texto, fecha_analisis
            ))
            continue

        # Preparación de Registro Sano (Conversión explícita de tipos para SQLite)
        tuplas_mensajes.append((
            source_id, run_id, msg_origen.autor, msg_origen.canal, msg_origen.tipo,
            msg_origen.texto, msg_ia.sentimiento, msg_ia.categoria_enrutamiento,
            msg_ia.regla_aplicada, msg_ia.puntuacion_relevancia.evidencia_explicita,
            msg_ia.puntuacion_relevancia.utilidad_comunitaria,
            msg_ia.puntuacion_relevancia.claridad_contexto,
            msg_ia.puntuacion_relevancia.total, msg_ia.motivo_seleccion,
            1 if msg_ia.requiere_soporte else 0, 
            1 if msg_ia.apto_para_publicacion else 0,
            fecha_analisis
        ))

        # Población de listas relacionales 1:N
        for tema in msg_ia.temas:
            tuplas_temas.append((source_id, tema))
        for entidad in msg_ia.entidades_relevantes:
            tuplas_entidades.append((source_id, entidad))

    conteo_sanos = len(tuplas_mensajes)
    conteo_rechazados = len(tuplas_dlq)

    # Bloque de Persistencia Atómica con Context Manager
    with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        cursor = conn.cursor()
        
        # El context manager ejecuta un BEGIN automático aquí
        
        # REGLA DE ORO: Purga preventiva integral por run_id para asegurar idempotencia limpia.
        # Gracias a ON DELETE CASCADE, esto limpia automáticamente mensajes, temas y entidades previos.
        cursor.execute("DELETE FROM lotes_procesamiento WHERE run_id = ?;", (run_id,))

        # Inserción de la cabecera analítica consolidada (Requerida para la FK)
        # Nota: Los totales reales calculados se guardan aquí.
        cursor.execute(
            """
            INSERT INTO lotes_procesamiento (
                run_id, request_id, origen_comunidad, periodo_referencia, 
                total_recibidos, total_validos, total_rechazados, fecha_procesamiento
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                run_id, request_id, 
                analisis_cognitivo.resumen_comunidad.sentimiento_predominante,  # Origen proxy
                "Semana_04",  # Mapeado temporal hasta integración final de metadatos
                conteo_sanos + conteo_rechazados, conteo_sanos, conteo_rechazados, fecha_analisis
            )
        )

        # Inserción masiva de Mensajes Sanos
        if tuplas_mensajes:
            cursor.executemany(
                """
                INSERT INTO mensajes_analizados (
                    source_id, run_id, autor, canal, tipo, texto_limpio, sentimiento,
                    categoria_enrutamiento, regla_aplicada, evidencia_explicita,
                    utilidad_comunitaria, claridad_contexto, puntuacion_total,
                    motivo_seleccion, requiere_soporte, apto_para_publicacion, fecha_analisis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                tuplas_mensajes
            )

        # Inserción masiva de Tablas Relacionales 1:N
        if tuplas_temas:
            cursor.executemany("INSERT INTO mensaje_temas (source_id, tema) VALUES (?, ?);", tuplas_temas)
        if tuplas_entidades:
            cursor.executemany("INSERT INTO mensaje_entidades (source_id, entidad) VALUES (?, ?);", tuplas_entidades)

        # Registro en la cola de descarte (DLQ / Auditoría de Rechazados)
        if tuplas_dlq:
            cursor.executemany(
                """
                INSERT INTO auditoria_rechazados (
                    run_id, request_id, mensaje_id, autor, canal, error_detalle, texto_crudo, fecha_isolation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                tuplas_dlq
            )
            
        # El context manager hace el COMMIT de forma automática al salir de este bloque si no hubo errores.

    logger.info(
        "insercion_analitica_bloque_idempotente_exitosa",
        run_id=run_id, registros_sanos=conteo_sanos, registros_desviados_dlq=conteo_rechazados
    )
    return conteo_sanos, conteo_rechazados



def _persistir_en_dlq(cursor: sqlite3.Cursor, tuplas_dlq: List[Tuple[Any, ...]]) -> None:
    """
    Función auxiliar para aislar y persistir de forma masiva los registros 
    corruptos en la cola de descarte (Dead Letter Queue).
    """
    if not tuplas_dlq:
        return

    cursor.executemany(
        """
        INSERT INTO auditoria_rechazados (
            run_id, request_id, mensaje_id, autor, canal, error_detalle, texto_crudo, fecha_aislamiento
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """,
        tuplas_dlq,
    )

def registrar_rechazados_validador(rechazados: List[Dict[str, Any]]) -> int:
    """
    Toma los diccionarios de descarte del validador, los adapta a tuplas
    y los persiste de forma atómica en la tabla de auditoría (DLQ).
    """
    if not rechazados:
        return 0

    tuplas_dlq = []
    for r in rechazados:
        tuplas_dlq.append((
            r.get("run_id"),
            r.get("request_id", "SIN_REQ_ID"),
            r.get("mensaje_id") or r.get("id"),
            r.get("autor", "Anónimo"),
            r.get("canal", "sin_canal"),
            r.get("error", "Error desconocido de validación"),
            r.get("texto_crudo", ""),
            r.get("fecha_aislamiento") or datetime.now(timezone.utc).isoformat()
        ))

    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        cursor = conn.cursor()
        
        cursor.execute("BEGIN TRANSACTION;")
        _persistir_en_dlq(cursor, tuplas_dlq)
        conn.commit()
        
        logger.info("persistencia_rechazados_validador_exitosa", total=len(tuplas_dlq))
        return len(tuplas_dlq)
    except Exception as e:
        conn.rollback()
        logger.error("error_persistencia_rechazados_validador", error=str(e))
        raise e
    finally:
        conn.close()

def inicializar_db() -> None:
    """
    Ejecuta el script DDL de forma segura para levantar el esqueleto 
    de tablas e índices en el archivo de base de datos local.
    """
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    try:
        cursor = conn.cursor()
        cursor.executescript(DDL_ESQUELETO)
        conn.commit()
        logger.info("inicializacion_base_de_datos_exitosa", path=DB_PATH)
    except Exception as e:
        logger.error("error_inicializacion_base_de_datos", error=str(e))
        raise e
    finally:
        conn.close()

def reconstruir_json_paquete_salida(run_id: str, numero_revision: int) -> Dict[str, Any]:
    """
    Reconstruye en memoria el objeto JSON exacto del paquete de salida
    haciendo un JOIN de las tablas relacionales de SQLite.
    Optimizado para evitar escrituras basura en disco.
    """
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        cursor = conn.cursor()

        # 1. Extraer datos maestros del paquete y sub-tablas nucleares
        cursor.execute("""
            SELECT 
                rc.total_interacciones_procesadas, rc.sentimiento_predominante,
                pl.titulo, pl.cuerpo, pl.canal_recomendado,
                rs.seccion, rs.titular, rs.resumen,
                eo.bucket, eo.ruta_objeto, eo.status_almacenamiento
            FROM paquetes_salida p
            JOIN resumen_comunidad rc ON p.run_id = rc.run_id
            LEFT JOIN post_linkedin pl ON p.run_id = pl.run_id AND p.numero_revision = pl.numero_revision
            LEFT JOIN resumen_semanal rs ON p.run_id = rs.run_id AND p.numero_revision = rs.numero_revision
            LEFT JOIN evidencia_almacenamiento_oci eo ON p.run_id = eo.run_id AND p.numero_revision = eo.numero_revision
            WHERE p.run_id = ? AND p.numero_revision = ?;
        """, (run_id, numero_revision))
        
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"No se encontró el paquete de salida para run_id='{run_id}' y revisión={numero_revision}")

        (total_int, sent_pred, li_titulo, li_cuerpo, li_canal, sem_sec, sem_tit, sem_res, oci_buck, oci_ruta, oci_stat) = row

        # 2. Extraer listas asociadas (Temas y Hashtags)
        cursor.execute("SELECT tema FROM resumen_temas_principales WHERE run_id = ? ORDER BY orden;", (run_id,))
        temas = [r[0] for r in cursor.fetchall()]

        cursor.execute("""
            SELECT hashtag FROM post_linkedin_hashtags 
            WHERE run_id = ? AND numero_revision = ?;
        """, (run_id, numero_revision))
        hashtags = [f"#{r[0].lstrip('#')}" for r in cursor.fetchall()]

        # 3. Construcción del Contrato Estructurado (Economía de tokens / RAM)
        return {
            "status": "exito",
            "resumen_comunidad": {
                "total_interacciones_procesadas": total_int,
                "sentimiento_predominante": sent_pred,
                "temas_principales": temas
            },
            "activos_distribucion_generados": {
                "post_linkedin": {
                    "titulo": li_titulo.strip() if li_titulo else "",
                    "copy": f"{li_cuerpo.strip()}\n\n{' '.join(hashtags)}".strip() if li_cuerpo else "",
                    "canal_recomendado": li_canal or "LinkedIn Oficial"
                },
                "destaque_newsletter_semanal": {
                    "seccion": sem_sec or "",
                    "titular": sem_tit or "",
                    "resumen": sem_res or ""
                }
            },
            "almacenamiento_oci": {
                "bucket": oci_buck or "communitylab-activos-marketing",
                "ruta_objeto": oci_ruta or "",
                "status": oci_stat or "no_iniciado"
            }
        }
    except Exception as e:
        logger.error("error_reconstruccion_json_memoria", run_id=run_id, revision=numero_revision, error=str(e))
        raise e
    finally:
        conn.close()

def sincronizar_pendientes_oci(storage_client: Any) -> Tuple[int, int]:
    """
    Barre la base de datos buscando paquetes con 'guardado_error' en OCI.
    Los reconstruye en memoria y reintenta la subida de forma idempotente.
    
    Retorna:
        Tuple[int, int]: Conteo de (sincronizaciones_exitosas, fallidas_recurrentes).
    """
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    cursor = conn.cursor()
    
    # 1. Identificar registros con fallos de almacenamiento previos
    cursor.execute("""
        SELECT e.run_id, e.numero_revision, l.periodo_referencia, l.request_id
        FROM evidencia_almacenamiento_oci e
        JOIN lotes_procesamiento l ON e.run_id = l.run_id
        WHERE e.status_almacenamiento = 'guardado_error';
    """)
    pendientes = cursor.fetchall()
    conn.close()

    exitosos = 0
    fallidos = 0

    if not pendientes:
        logger.info("sincronizacion_oci_sin_pendientes", detalle="La cola de reintentos está vacía.")
        return 0, 0

    logger.info("sincronizacion_oci_iniciada", total_lote=len(pendientes))

    # 2. Procesar el lote de reintentos
    for run_id, revision, periodo, request_id in pendientes:
        try:
            # Reconstrucción relacional dinámica en memoria RAM
            payload = reconstruir_json_paquete_salida(run_id, revision)
            
            # Despachar al cliente idempotente de OCI
            resultado = storage_client.upload_package(
                package_payload=payload,
                periodo_referencia=periodo,
                run_id=run_id,
                numero_revision=revision,
                request_id=request_id
            )

            # 3. Evaluar resultado y actualizar de forma atómica en SQLite
            if resultado.status_almacenamiento == "guardado_con_exito":
                with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS) as conn_update:
                    conn_update.execute("""
                        UPDATE evidencia_almacenamiento_oci
                        SET status_almacenamiento = 'guardado_con_exito',
                            comprobacion_lectura = 1
                        WHERE run_id = ? AND numero_revision = ?;
                    """, (run_id, revision))
                    conn_update.commit()
                
                exitosos += 1
                logger.info("reintento_sincronizacion_oci_exitoso", run_id=run_id, revision=revision)
            else:
                fallidos += 1
                logger.warning("reintento_sincronizacion_oci_fallido_recurrente", run_id=run_id, error=resultado.mensaje_error)

        except Exception as e:
            fallidos += 1
            logger.error("error_grave_iteracion_sincronizador", run_id=run_id, error=str(e))
            continue

    logger.info("sincronizacion_oci_finalizada", exitosos=exitosos, fallidos=fallidos)
    return exitosos, fallidos
