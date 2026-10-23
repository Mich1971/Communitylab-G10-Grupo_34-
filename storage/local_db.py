"""
M├│dulo de Persistencia Local (Data Engineering).
Gesti├│n de SQLite con esqueleto de 19 tablas, optimizado para alta concurrencia.
"""

import os
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import structlog

logger = structlog.get_logger("storage.local_db")

DB_PATH = os.getenv("SQLITE_DB_PATH", "data/communitylab.db")
DB_TIMEOUT_SEGUNDOS = 30.0  # Evita excepciones de "database is locked" por concurrencia

# =============================================================================
# SCRIPT DDL OPTIMIZADO (Corregido contra fallos de cascada y bloqueo de IA)
# =============================================================================
DDL_ESQUELETO = """
-- 1. MAESTRO DE LOTES Y AUDITOR├ìA (Contrato 1)
CREATE TABLE IF NOT EXISTS lotes_procesamiento (
    run_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    origen_comunidad TEXT NOT NULL,
    periodo_referencia TEXT NOT NULL,
    total_recibidos INTEGER NOT NULL,
    total_validos INTEGER NOT NULL,
    total_rechazados INTEGER NOT NULL,
    fecha_procesamiento TEXT NOT NULL,
    estado_flujo TEXT NOT NULL DEFAULT 'procesando'
);

-- 2. TABLA NUCLEAR DE ANAL├ìTICA (Contrato 2)
-- OPTIMIZACI├ôN: Se remueve el CHECK matem├ítico r├¡gido. La validaci├│n num├®rica
-- se delega a Pydantic en Python para evitar que un desfase menor del LLM tire la transacci├│n.
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

-- 3-4. TEMAS Y ENTIDADES
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

-- 5. S├ìNTESIS AGREGADA POR LOTE (Contrato 2)
CREATE TABLE IF NOT EXISTS resumen_comunidad (
    run_id TEXT PRIMARY KEY,
    total_interacciones_procesadas INTEGER NOT NULL CHECK (total_interacciones_procesadas >= 1),
    registros_validos INTEGER NOT NULL CHECK (registros_validos >= 1),
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

-- 6. FLUJO DE CURADUR├ìA Y PUBLICACI├ôN (Contrato 3)
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

-- CORRECCI├ôN: Se a├▒ade 'ON DELETE CASCADE' hacia mensajes_analizados para evitar bloqueos por llaves for├íneas huerfanas.
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

-- CORRECCI├ôN: Se a├▒ade 'ON DELETE CASCADE' hacia mensajes_analizados para garantizar consistencia.
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

-- 7. TELEMETR├ìA OPERATIVA
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

-- 8. AUDITOR├ìA DE RECHAZADOS
-- ECONOM├ìA DE TOKENS/SOBRECARGA: Se unifica la columna de fecha en base de datos.
CREATE TABLE IF NOT EXISTS auditoria_rechazados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    request_id TEXT NOT NULL,
    mensaje_id TEXT,
    autor TEXT,
    canal TEXT,
    error_detalle TEXT NOT NULL,
    texto_crudo TEXT,
    fecha_aislamiento TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- ├ìNDICES ESTRAT├ëGICOS (Optimizados para Joins concurrentes de la UI)
CREATE INDEX IF NOT EXISTS idx_analisis_sentimiento ON mensajes_analizados (sentimiento);
CREATE INDEX IF NOT EXISTS idx_analisis_enrutamiento ON mensajes_analizados (categoria_enrutamiento);
CREATE INDEX IF NOT EXISTS idx_analisis_puntuacion ON mensajes_analizados (puntuacion_total DESC);
CREATE INDEX IF NOT EXISTS idx_analisis_soporte ON mensajes_analizados (requiere_soporte) WHERE requiere_soporte = 1;
CREATE INDEX IF NOT EXISTS idx_temas_topico ON mensaje_temas (tema);
CREATE INDEX IF NOT EXISTS idx_entidades_nombre ON mensaje_entidades (entidad);
CREATE INDEX IF NOT EXISTS idx_telemetria_run ON telemetria_llamadas_llm (run_id, proveedor);
CREATE INDEX IF NOT EXISTS idx_estado_revision_pendientes ON estado_revision (estado) WHERE estado = 'pendiente';

-- OPTIMIZACI├ôN: ├ìndice compuesto estrat├®gico para acelerar el renderizado de la cola de curadur├¡a de Sergio
CREATE INDEX IF NOT EXISTS idx_paquetes_revision ON paquetes_salida (run_id, numero_revision);
-- VISTAS ANAL├ìTICAS (Sin cambios, beneficiadas por los nuevos ├¡ndices y cascadas)
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
def _persistir_en_dlq(cursor: sqlite3.Cursor, tuplas_dlq: List[Tuple[Any, ...]]) -> None:
    """
    Funci├│n auxiliar para aislar y persistir de forma masiva los registros
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


def registrar_analisis_bloque(
    analisis_cognitivo: Any, interacciones_origen: List[Any]
) -> Tuple[int, int]:
    """
    Inserta en bloque y de forma at├│mica los resultados anal├¡ticos de la IA.
    Aplica purga preventiva para garantizar la idempotencia y un DLQ para aislamiento.

    Retorna:
        Tuple[int, int]: Conteo exacto de (registros_sanos, registros_rechazados).
    """
    run_id = analisis_cognitivo.run_id
    request_id = analisis_cognitivo.request_id
    fecha_analisis = datetime.now(timezone.utc).isoformat()

    dict_origen = {int_val.id: int_val for int_val in interacciones_origen}

    tuplas_mensajes = []
    tuplas_temas = []
    tuplas_entidades = []
    tuplas_purga = []
    tuplas_dlq = []

    for msg_ia in analisis_cognitivo.mensajes_analizados:
        source_id = msg_ia.source_id
        msg_origen = dict_origen.get(source_id)

        # DLQ Caso 1: ID Hu├®rfano
        if not msg_origen:
            logger.error("error_integridad_origen", source_id=source_id, run_id=run_id)
            tuplas_dlq.append((
                run_id, request_id, source_id, "Desconocido", "Desconocido",
                "ERROR_INTEGRIDAD: El ID analizado por la IA no existe en la ingesta original.",
                f"Motivo IA: {msg_ia.motivo_seleccion}", fecha_analisis
            ))
            continue

        # DLQ Caso 2: Violaci├│n Check Longitud (M├¡nimo 10 caracteres)
        if len(msg_ia.motivo_seleccion) < 10:
            logger.error("error_validacion_ddl_motivo", source_id=source_id, run_id=run_id)
            tuplas_dlq.append((
                run_id, request_id, source_id, msg_origen.autor, msg_origen.canal,
                "ERROR_DDL_CHECK: 'motivo_seleccion' inferior a 10 caracteres.",
                msg_origen.texto, fecha_analisis
            ))
            continue

        # Registro Sano: Preparaci├│n para Persistencia Anal├¡tica
        tuplas_purga.append((source_id,))
        tuplas_mensajes.append((
            source_id, run_id, msg_origen.autor, msg_origen.canal, msg_origen.tipo,
            msg_origen.texto, msg_ia.sentimiento, msg_ia.categoria_enrutamiento,
            msg_ia.regla_aplicada, msg_ia.puntuacion_relevancia.evidencia_explicita,
            msg_ia.puntuacion_relevancia.utilidad_comunitaria,
            msg_ia.puntuacion_relevancia.claridad_contexto,
            msg_ia.puntuacion_relevancia.total, msg_ia.motivo_seleccion,
            1 if msg_ia.requiere_soporte else 0, 1 if msg_ia.apto_para_publicacion else 0,
            fecha_analisis,
        ))

        for tema in msg_ia.temas:
            tuplas_temas.append((source_id, tema))
        for entidad in msg_ia.entidades_relevantes:
            tuplas_entidades.append((source_id, entidad))

    conteo_sanos = len(tuplas_mensajes)
    conteo_rechazados = len(tuplas_dlq)

    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")

        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION;")

        # 1. Purga preventiva at├│mica de tablas secundarias
        if tuplas_purga:
            cursor.executemany("DELETE FROM mensaje_temas WHERE source_id = ?;", tuplas_purga)
            cursor.executemany("DELETE FROM mensaje_entidades WHERE source_id = ?;", tuplas_purga)

        # 2. Inserci├│n de Mensajes Procesados Correctamente
        if tuplas_mensajes:
            cursor.executemany(
                """
                INSERT OR REPLACE INTO mensajes_analizados (
                    source_id, run_id, autor, canal, tipo, texto_limpio, sentimiento,
                    categoria_enrutamiento, regla_aplicada, evidencia_explicita,
                    utilidad_comunitaria, claridad_contexto, puntuacion_total,
                    motivo_seleccion, requiere_soporte, apto_para_publicacion, fecha_analisis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
                tuplas_mensajes,
            )

        # 3. Inserci├│n de Metadatos Relacionales
        if tuplas_temas:
            cursor.executemany("INSERT INTO mensaje_temas (source_id, tema) VALUES (?, ?);", tuplas_temas)
        if tuplas_entidades:
            cursor.executemany("INSERT INTO mensaje_entidades (source_id, entidad) VALUES (?, ?);", tuplas_entidades)

        # 4. Aislamiento Masivo en la Cola de Descarte (DLQ)
        _persistir_en_dlq(cursor, tuplas_dlq)

        conn.commit()
        logger.info(
            "insercion_analitica_bloque_con_dlq_exitosa",
            run_id=run_id, registros_sanos=conteo_sanos, registros_desviados_dlq=conteo_rechazados
        )
        return conteo_sanos, conteo_rechazados

    except Exception as e:
        conn.rollback()
        logger.error("error_transaccion_bloque_dlq", run_id=run_id, error=str(e))
        raise e
    finally:
        conn.close()

def registrar_rechazados_validador(rechazados: List[Dict[str, Any]]) -> int:
    """
    Toma los diccionarios de descarte del validador, los adapta a tuplas
    y los persiste de forma at├│mica en la tabla de auditor├¡a (DLQ).
    """
    if not rechazados:
        return 0

    tuplas_dlq = []
    for r in rechazados:
        tuplas_dlq.append((
            r.get("run_id"),
            r.get("request_id", "SIN_REQ_ID"),
            r.get("mensaje_id") or r.get("id"),
            r.get("autor", "An├│nimo"),
            r.get("canal", "sin_canal"),
            r.get("error", "Error desconocido de validaci├│n"),
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

def _migrar_estado_flujo(conn: sqlite3.Connection) -> None:
    """A├▒ade `lotes_procesamiento.estado_flujo` a bases creadas antes de la spec v3.1 (┬º8.8).

    `CREATE TABLE IF NOT EXISTS` no altera tablas existentes, por eso se migra aparte.
    """
    columnas = {fila[1] for fila in conn.execute("PRAGMA table_info(lotes_procesamiento);")}
    if "estado_flujo" not in columnas:
        conn.execute(
            "ALTER TABLE lotes_procesamiento "
            "ADD COLUMN estado_flujo TEXT NOT NULL DEFAULT 'procesando';"
        )


def inicializar_db() -> None:
    """
    Ejecuta el script DDL de forma segura para levantar el esqueleto
    de tablas e ├¡ndices en el archivo de base de datos local.
    """
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SEGUNDOS)
    try:
        cursor = conn.cursor()
        cursor.executescript(DDL_ESQUELETO)
        _migrar_estado_flujo(conn)
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
            raise ValueError(f"No se encontr├│ el paquete de salida para run_id='{run_id}' y revisi├│n={numero_revision}")

        (total_int, sent_pred, li_titulo, li_cuerpo, li_canal, sem_sec, sem_tit, sem_res, oci_buck, oci_ruta, oci_stat) = row

        # 2. Extraer listas asociadas (Temas y Hashtags)
        cursor.execute("SELECT tema FROM resumen_temas_principales WHERE run_id = ? ORDER BY orden;", (run_id,))
        temas = [r[0] for r in cursor.fetchall()]

        cursor.execute("""
            SELECT hashtag FROM post_linkedin_hashtags
            WHERE run_id = ? AND numero_revision = ?;
        """, (run_id, numero_revision))
        hashtags = [f"#{r[0].lstrip('#')}" for r in cursor.fetchall()]

        # 3. Construcci├│n del Contrato Estructurado (Econom├¡a de tokens / RAM)
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
        logger.info("sincronizacion_oci_sin_pendientes", detalle="La cola de reintentos est├í vac├¡a.")
        return 0, 0

    logger.info("sincronizacion_oci_iniciada", total_lote=len(pendientes))

    # 2. Procesar el lote de reintentos
    for run_id, revision, periodo, request_id in pendientes:
        try:
            # Reconstrucci├│n relacional din├ímica en memoria RAM
            payload = reconstruir_json_paquete_salida(run_id, revision)

            # Despachar al cliente idempotente de OCI
            resultado = storage_client.upload_package(
                package_payload=payload,
                periodo_referencia=periodo,
                run_id=run_id,
                numero_revision=revision,
                request_id=request_id
            )

            # 3. Evaluar resultado y actualizar de forma at├│mica en SQLite
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


# =============================================================================
# GESTOR ORIENTADO A OBJETOS PARA EL ORQUESTADOR (spec pipeline_runner v3.1)
# =============================================================================
class LocalDatabaseManager:
    """Fachada transaccional de SQLite para `orchestration/pipeline_runner.py`.

    Cada m├®todo abre su propia conexi├│n y escribe dentro de un bloque at├│mico
    (`with conn:`): o se confirma completo o se revierte (secci├│n 8, punto 3).
    Todas las escrituras son idempotentes por `run_id`: el worker puede
    reejecutar un lote tras una ca├¡da sin duplicar filas (secci├│n 5.4).
    """

    def __init__(self, db_path: Any = None) -> None:
        self.db_path = str(db_path) if db_path else DB_PATH
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=DB_TIMEOUT_SEGUNDOS)
        try:
            # WAL antes del DDL: evita un fsync por sentencia del modo rollback-journal.
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript(DDL_ESQUELETO)
            _migrar_estado_flujo(conn)
            conn.commit()
        finally:
            conn.close()

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=DB_TIMEOUT_SEGUNDOS)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        # Con WAL, NORMAL es el ajuste recomendado: sigue siendo seguro ante cierres
        # inesperados de la aplicaci├│n y evita un fsync por cada commit.
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    # -- lotes y rechazados -------------------------------------------------

    def registrar_lote(
        self,
        run_id: str,
        request_id: str,
        origen_comunidad: str,
        periodo_referencia: str,
        total_recibidos: int,
        total_validos: int,
        total_rechazados: int,
        rechazados: Any = None,
        conn: Any = None,
    ) -> None:
        """Upsert de `lotes_procesamiento`; si `rechazados` no es None, reemplaza
        las filas de `auditoria_rechazados` de este `run_id` (idempotente).

        Si se recibe `conn`, se escribe dentro de esa transacci├│n (la confirma o
        revierte quien la abri├│); si no, abre y confirma la suya."""
        ahora = datetime.now(timezone.utc).isoformat()
        propia = conn is None
        if propia:
            conn = self._conectar()
        try:
            with (conn if propia else nullcontext()):
                conn.execute(
                    """
                    INSERT INTO lotes_procesamiento (
                        run_id, request_id, origen_comunidad, periodo_referencia,
                        total_recibidos, total_validos, total_rechazados, fecha_procesamiento
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        origen_comunidad = excluded.origen_comunidad,
                        periodo_referencia = excluded.periodo_referencia,
                        total_recibidos = excluded.total_recibidos,
                        total_validos = excluded.total_validos,
                        total_rechazados = excluded.total_rechazados,
                        fecha_procesamiento = excluded.fecha_procesamiento;
                    """,
                    (
                        run_id, request_id, origen_comunidad, periodo_referencia,
                        total_recibidos, total_validos, total_rechazados, ahora,
                    ),
                )
                if rechazados is not None:
                    conn.execute("DELETE FROM auditoria_rechazados WHERE run_id = ?;", (run_id,))
                    tuplas = [
                        (
                            run_id,
                            r.get("request_id") or request_id,
                            r.get("mensaje_id") or r.get("id"),
                            r.get("autor", "An├│nimo"),
                            r.get("canal", "sin_canal"),
                            r.get("error_detalle") or r.get("error") or "Error desconocido de validaci├│n",
                            r.get("texto_crudo", ""),
                            r.get("fecha_aislamiento") or ahora,
                        )
                        for r in rechazados
                    ]
                    _persistir_en_dlq(conn.cursor(), tuplas)
            logger.info("lote_registrado", run_id=run_id, total_validos=total_validos,
                        total_rechazados=total_rechazados)
        finally:
            if propia:
                conn.close()

    def actualizar_estado_flujo(self, run_id: str, estado_flujo: str) -> None:
        """Registra el estado terminal del lote para consultas hist├│ricas (spec ┬º8.8)."""
        conn = self._conectar()
        try:
            with conn:
                conn.execute(
                    "UPDATE lotes_procesamiento SET estado_flujo = ? WHERE run_id = ?;",
                    (estado_flujo, run_id),
                )
        finally:
            conn.close()

    def leer_rechazados(self, run_id: str) -> List[Dict[str, Any]]:
        """Relee las filas de `auditoria_rechazados` de un `run_id`."""
        conn = self._conectar()
        try:
            filas = conn.execute(
                "SELECT mensaje_id, autor, canal, error_detalle FROM auditoria_rechazados "
                "WHERE run_id = ? ORDER BY id;",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"mensaje_id": f[0], "autor": f[1], "canal": f[2], "error_detalle": f[3]}
            for f in filas
        ]

    # -- an├ílisis cognitivo ---------------------------------------------------

    def registrar_analisis(self, analisis: Any, interacciones: List[Any], conn: Any = None) -> int:
        """Inserta at├│micamente `mensajes_analizados`, `mensaje_temas`,
        `mensaje_entidades` y las tablas `resumen_*` del lote. Devuelve el
        n├║mero de mensajes persistidos. Requiere que el lote ya exista.
        Con `conn` participa en una transacci├│n ajena (ver `registrar_lote`)."""
        run_id = analisis.run_id
        fecha = datetime.now(timezone.utc).isoformat()
        origen = {i.id: i for i in interacciones}
        resumen = analisis.resumen_comunidad

        propia = conn is None
        if propia:
            conn = self._conectar()
        try:
            with (conn if propia else nullcontext()):
                conn.execute("DELETE FROM mensajes_analizados WHERE run_id = ?;", (run_id,))
                conn.execute("DELETE FROM resumen_comunidad WHERE run_id = ?;", (run_id,))
                conn.execute("DELETE FROM resumen_distribucion_sentimiento WHERE run_id = ?;", (run_id,))
                conn.execute("DELETE FROM resumen_temas_principales WHERE run_id = ?;", (run_id,))
                conn.execute("DELETE FROM resumen_alertas_soporte WHERE run_id = ?;", (run_id,))

                persistidos = 0
                for m in analisis.mensajes_analizados:
                    src = origen.get(m.source_id)
                    if src is None:
                        logger.error("error_integridad_origen", source_id=m.source_id, run_id=run_id)
                        continue
                    conn.execute("DELETE FROM mensaje_temas WHERE source_id = ?;", (m.source_id,))
                    conn.execute("DELETE FROM mensaje_entidades WHERE source_id = ?;", (m.source_id,))
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO mensajes_analizados (
                            source_id, run_id, autor, canal, tipo, texto_limpio, sentimiento,
                            categoria_enrutamiento, regla_aplicada, evidencia_explicita,
                            utilidad_comunitaria, claridad_contexto, puntuacion_total,
                            motivo_seleccion, requiere_soporte, apto_para_publicacion, fecha_analisis
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            m.source_id, run_id, src.autor, src.canal, src.tipo, src.texto,
                            m.sentimiento, m.categoria_enrutamiento, m.regla_aplicada,
                            m.puntuacion_relevancia.evidencia_explicita,
                            m.puntuacion_relevancia.utilidad_comunitaria,
                            m.puntuacion_relevancia.claridad_contexto,
                            m.puntuacion_relevancia.total, m.motivo_seleccion,
                            1 if m.requiere_soporte else 0,
                            1 if m.apto_para_publicacion else 0, fecha,
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO mensaje_temas (source_id, tema) VALUES (?, ?);",
                        [(m.source_id, t) for t in m.temas],
                    )
                    conn.executemany(
                        "INSERT INTO mensaje_entidades (source_id, entidad) VALUES (?, ?);",
                        [(m.source_id, e) for e in m.entidades_relevantes],
                    )
                    persistidos += 1

                conn.execute(
                    """
                    INSERT INTO resumen_comunidad (
                        run_id, total_interacciones_procesadas, registros_validos,
                        registros_rechazados, sentimiento_predominante, fecha_generacion
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        run_id, resumen.total_interacciones_procesadas, resumen.registros_validos,
                        resumen.registros_rechazados, resumen.sentimiento_predominante, fecha,
                    ),
                )
                conn.executemany(
                    "INSERT INTO resumen_distribucion_sentimiento (run_id, sentimiento, conteo) VALUES (?, ?, ?);",
                    [(run_id, s, c) for s, c in resumen.distribucion_sentimiento.items()],
                )
                conn.executemany(
                    "INSERT INTO resumen_temas_principales (run_id, tema, orden) VALUES (?, ?, ?);",
                    [(run_id, t, i) for i, t in enumerate(resumen.temas_principales, start=1)],
                )
                conn.executemany(
                    "INSERT INTO resumen_alertas_soporte (run_id, alerta) VALUES (?, ?);",
                    [(run_id, a) for a in resumen.alertas_soporte],
                )
            logger.info("analisis_registrado", run_id=run_id, mensajes=persistidos)
            return persistidos
        finally:
            if propia:
                conn.close()

    def registrar_lote_y_analisis(
        self,
        analisis: Any,
        interacciones: List[Any],
        origen_comunidad: str,
        periodo_referencia: str,
        total_recibidos: int,
        total_rechazados: int,
    ) -> int:
        """Bloque at├│mico ├║nico (`with connection:`) con `lotes_procesamiento`,
        `mensajes_analizados` y `mensaje_temas` (spec, secci├│n 8, punto 3)."""
        conn = self._conectar()
        try:
            with conn:
                self.registrar_lote(
                    analisis.run_id, analisis.request_id, origen_comunidad, periodo_referencia,
                    total_recibidos, len(interacciones), total_rechazados, conn=conn,
                )
                return self.registrar_analisis(analisis, interacciones, conn=conn)
        finally:
            conn.close()

    # -- paquete de salida (Contrato 3) --------------------------------------

    def registrar_paquete(self, paquete: Any) -> None:
        """Persiste at├│micamente el `PaqueteSalida` (borrador de curadur├¡a) y la
        evidencia de OCI. Reemplaza la misma `(run_id, numero_revision)`."""
        run_id = paquete.run_id
        num = paquete.revision.numero_revision
        meta = paquete.metadatos_ejecucion
        pl = paquete.activos.post_linkedin
        rs = paquete.activos.resumen_semanal
        rev = paquete.revision
        oci = paquete.almacenamiento_oci

        conn = self._conectar()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM paquetes_salida WHERE run_id = ? AND numero_revision = ?;",
                    (run_id, num),
                )
                conn.execute(
                    "INSERT INTO paquetes_salida (run_id, numero_revision, request_id, schema_version, "
                    "fecha_generacion) VALUES (?, ?, ?, ?, ?);",
                    (run_id, num, paquete.request_id, paquete.schema_version,
                     paquete.fecha_generacion.isoformat()),
                )
                conn.execute(
                    "INSERT INTO metadatos_ejecucion_paquete (run_id, numero_revision, modelo, "
                    "prompt_version, latencia_ms, tokens_totales) VALUES (?, ?, ?, ?, ?, ?);",
                    (run_id, num, meta.modelo, meta.prompt_version, meta.latencia_ms, meta.tokens_totales),
                )
                conn.execute(
                    "INSERT INTO post_linkedin (run_id, numero_revision, titulo, canal_recomendado, cuerpo) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (run_id, num, pl.titulo, pl.canal_recomendado, pl.cuerpo),
                )
                conn.executemany(
                    "INSERT INTO post_linkedin_hashtags (run_id, numero_revision, hashtag) VALUES (?, ?, ?);",
                    [(run_id, num, h) for h in pl.hashtags],
                )
                conn.executemany(
                    "INSERT INTO post_linkedin_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, ?);",
                    [(run_id, num, s) for s in pl.source_ids],
                )
                conn.execute(
                    "INSERT INTO resumen_semanal (run_id, numero_revision, seccion, titular, resumen) "
                    "VALUES (?, ?, ?, ?, ?);",
                    (run_id, num, rs.seccion, rs.titular, rs.resumen),
                )
                conn.executemany(
                    "INSERT INTO resumen_semanal_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, ?);",
                    [(run_id, num, s) for s in rs.source_ids],
                )
                conn.execute(
                    "INSERT INTO estado_revision (run_id, numero_revision, estado, revisor, fecha_decision, "
                    "comentarios) VALUES (?, ?, ?, ?, ?, ?);",
                    (run_id, num, rev.estado, rev.revisor,
                     rev.fecha_decision.isoformat() if rev.fecha_decision else None, rev.comentarios),
                )
                conn.execute(
                    "INSERT INTO evidencia_almacenamiento_oci (run_id, numero_revision, bucket, ruta_objeto, "
                    "status_almacenamiento, comprobacion_lectura) VALUES (?, ?, ?, ?, ?, ?);",
                    (run_id, num, oci.bucket, oci.ruta_objeto, oci.status_almacenamiento,
                     1 if oci.comprobacion_lectura else 0),
                )
            logger.info("paquete_registrado", run_id=run_id, revision=num,
                        status_oci=oci.status_almacenamiento)
        finally:
            conn.close()
