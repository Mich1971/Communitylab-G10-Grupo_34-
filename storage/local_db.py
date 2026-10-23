"""Módulo de Persistencia Local (Data Engineering) — versión 2 del esquema.

Gestión de SQLite con esqueleto de 19 tablas, alta concurrencia, idempotencia
ante reejecuciones del worker y cumplimiento estricto de `esquemas.py`.

Cambios respecto a la versión 1 (revisión técnica del 19-oct-2026)
-----------------------------------------------------------------
1. `mensajes_analizados` pasa a clave compuesta `(run_id, source_id)`. Con la
   clave global, dos runs que compartían un id nativo (p. ej. ventanas
   semanales solapadas) se pisaban entre sí, y el `INSERT OR REPLACE` +
   `ON DELETE CASCADE` borraba en silencio los `source_ids` del paquete del
   primer run. Las tablas hijas (`mensaje_temas`, `mensaje_entidades`,
   `post_linkedin_source_ids`, `resumen_semanal_source_ids`) referencian ahora
   `(run_id, source_id)`. Las FK hacia `mensajes_analizados` de las tablas de
   `source_ids` NO llevan CASCADE (como en el diseño original de la base).
2. Una sola transacción por lote: `persistir_resultado(...)` hace lote,
   rechazos, análisis, paquete y telemetría de forma atómica (recomendación 3
   de la spec del orquestador). Las funciones sueltas se conservan.
3. `COMMUNITYLAB_DATA_DIR` tiene prioridad sobre `SQLITE_DB_PATH`, igual que en
   `api/config.py`: API, worker y orquestador usan siempre el mismo archivo.
4. `auditoria_rechazados.origen` ('validador' | 'analisis') sustituye al
   prefijo del texto del error para distinguir ambos tipos de descarte.
5. `registrar_rechazados_validador` exige `run_id` (sin él no es idempotente).
6. `registrar_telemetria` persiste `telemetria_llamadas_llm`.
7. `reconstruir_json_paquete_salida` ordena de forma determinista, ya no
   inventa valores y valida el resultado contra `PaqueteSalida` antes de
   devolverlo (nunca se sube a OCI un JSON inválido).
8. Migración automática v1 -> v2 en `inicializar_db` (conserva los datos).
9. Reconciliado con la versión de Andrea (`local_db_2`): `CHECK (>= 0)` en los
   totales de `lotes_procesamiento` (lotes vacíos), prefijo `ERROR_NEGOCIO_CHECK`
   para motivos < 10 caracteres y reparación de bases creadas con el typo
   `fecha_isolation` (se renombra a `fecha_aislamiento`).
10. `LocalDatabaseManager` expone todos los métodos requeridos por
    `orchestration/pipeline_runner.py` (`registrar_lote`,
    `registrar_lote_y_analisis`, `registrar_paquete`, `leer_rechazados`,
    `actualizar_estado_flujo`) además de `persistir_resultado`,
    `reconstruir_json_paquete_salida` y `sincronizar_pendientes_oci`.

Orden lógico de persistencia de un lote (lo respeta `persistir_resultado`):
lote -> rechazos del validador -> análisis -> paquete -> telemetría.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from esquemas import PaqueteSalida

logger = structlog.get_logger("storage.local_db")

# Constante de compatibilidad (los tests antiguos hacen monkeypatch de esto).
DB_PATH = "data/communitylab.db"
DB_TIMEOUT_SEGUNDOS = 30.0  # Evita "database is locked" por concurrencia
VERSION_ESQUEMA_DB = 2

_VISTAS = (
    "vista_resumen_sentimiento",
    "vista_temas_tendencia",
    "vista_alertas_soporte",
    "vista_candidatos_publicacion",
    "vista_telemetria_lote",
    "vista_cola_curaduria",
)


def _resolver_db_path(db_path: str | Path | None = None) -> str:
    """Resuelve la ruta a SQLite. Prioridad:

    1. Parámetro explícito `db_path`.
    2. `COMMUNITYLAB_DATA_DIR` + `/communitylab.db` (igual que `api/config.py`).
    3. `SQLITE_DB_PATH` (obsoleta; solo si no hay `COMMUNITYLAB_DATA_DIR`).
    4. Constante `DB_PATH`.
    """
    if db_path is not None:
        ruta = Path(db_path)
    elif "COMMUNITYLAB_DATA_DIR" in os.environ:
        ruta = Path(os.environ["COMMUNITYLAB_DATA_DIR"]) / "communitylab.db"
    elif "SQLITE_DB_PATH" in os.environ:
        ruta = Path(os.environ["SQLITE_DB_PATH"])
    else:
        ruta = Path(DB_PATH)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    return str(ruta)


# =============================================================================
# DDL v2
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
    fecha_procesamiento TEXT NOT NULL,
    estado_flujo TEXT NOT NULL DEFAULT 'iniciado'
);

-- 2. TABLA NUCLEAR DE ANALÍTICA (Contrato 2). Clave compuesta: el mismo
--    source_id puede aparecer en runs distintos sin pisarse.
CREATE TABLE IF NOT EXISTS mensajes_analizados (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
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
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- 3-4. TEMAS Y ENTIDADES
CREATE TABLE IF NOT EXISTS mensaje_temas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tema TEXT NOT NULL,
    FOREIGN KEY (run_id, source_id) REFERENCES mensajes_analizados (run_id, source_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mensaje_entidades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    entidad TEXT NOT NULL,
    FOREIGN KEY (run_id, source_id) REFERENCES mensajes_analizados (run_id, source_id) ON DELETE CASCADE
);

-- 5. SÍNTESIS AGREGADA POR LOTE (Contrato 2)
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

-- 6. FLUJO DE CURADURÍA Y PUBLICACIÓN (Contrato 3)
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
    FOREIGN KEY (run_id, source_id) REFERENCES mensajes_analizados (run_id, source_id)
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
    FOREIGN KEY (run_id, source_id) REFERENCES mensajes_analizados (run_id, source_id)
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

-- 8. AUDITORÍA DE RECHAZADOS (origen: 'validador' | 'analisis')
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
    origen TEXT NOT NULL DEFAULT 'validador',
    FOREIGN KEY (run_id) REFERENCES lotes_procesamiento (run_id) ON DELETE CASCADE
);

-- ÍNDICES ESTRATÉGICOS
CREATE INDEX IF NOT EXISTS idx_analisis_sentimiento ON mensajes_analizados (sentimiento);
CREATE INDEX IF NOT EXISTS idx_analisis_enrutamiento ON mensajes_analizados (categoria_enrutamiento);
CREATE INDEX IF NOT EXISTS idx_analisis_puntuacion ON mensajes_analizados (puntuacion_total DESC);
CREATE INDEX IF NOT EXISTS idx_analisis_soporte ON mensajes_analizados (requiere_soporte) WHERE requiere_soporte = 1;
CREATE INDEX IF NOT EXISTS idx_temas_topico ON mensaje_temas (tema);
CREATE INDEX IF NOT EXISTS idx_temas_mensaje ON mensaje_temas (run_id, source_id);
CREATE INDEX IF NOT EXISTS idx_entidades_nombre ON mensaje_entidades (entidad);
CREATE INDEX IF NOT EXISTS idx_entidades_mensaje ON mensaje_entidades (run_id, source_id);
CREATE INDEX IF NOT EXISTS idx_telemetria_run ON telemetria_llamadas_llm (run_id, proveedor);
CREATE INDEX IF NOT EXISTS idx_estado_revision_pendientes ON estado_revision (estado) WHERE estado = 'pendiente';
CREATE INDEX IF NOT EXISTS idx_paquetes_revision ON paquetes_salida (run_id, numero_revision);
CREATE INDEX IF NOT EXISTS idx_auditoria_run ON auditoria_rechazados (run_id, origen);

-- VISTAS ANALÍTICAS (las columnas nuevas `run_id` van al final para no romper consultas existentes)
CREATE VIEW IF NOT EXISTS vista_resumen_sentimiento AS
SELECT m.run_id, l.periodo_referencia, m.sentimiento, COUNT(m.source_id) AS total_mensajes, ROUND(AVG(m.puntuacion_total), 2) AS promedio_relevancia
FROM mensajes_analizados m JOIN lotes_procesamiento l ON m.run_id = l.run_id GROUP BY m.run_id, l.periodo_referencia, m.sentimiento;

CREATE VIEW IF NOT EXISTS vista_temas_tendencia AS
SELECT t.tema, COUNT(*) AS frecuencia, ROUND(AVG(m.puntuacion_total), 2) AS relevancia_media
FROM mensaje_temas t JOIN mensajes_analizados m ON t.run_id = m.run_id AND t.source_id = m.source_id GROUP BY t.tema;

CREATE VIEW IF NOT EXISTS vista_alertas_soporte AS
SELECT source_id, autor, canal, texto_limpio, motivo_seleccion, fecha_analisis, run_id
FROM mensajes_analizados WHERE requiere_soporte = 1 OR sentimiento = 'Dificultad/Negativo';

CREATE VIEW IF NOT EXISTS vista_candidatos_publicacion AS
SELECT source_id, autor, canal, categoria_enrutamiento, puntuacion_total, motivo_seleccion, texto_limpio, run_id
FROM mensajes_analizados WHERE apto_para_publicacion = 1 AND categoria_enrutamiento = 'Logro' AND puntuacion_total >= 4;

CREATE VIEW IF NOT EXISTS vista_telemetria_lote AS
SELECT run_id, proveedor, modelo, COUNT(*) AS llamadas, ROUND(AVG(latencia_ms), 1) AS latencia_media_ms, SUM(tokens_totales) AS tokens_totales_lote, SUM(tokens_cacheados) AS tokens_cacheados_lote
FROM telemetria_llamadas_llm GROUP BY run_id, proveedor, modelo;

CREATE VIEW IF NOT EXISTS vista_cola_curaduria AS
SELECT p.run_id, p.numero_revision, p.fecha_generacion, pl.titulo AS titulo_linkedin, pl.cuerpo AS cuerpo_linkedin, rs.titular AS titular_resumen_semanal, rs.resumen AS resumen_semanal, er.estado, er.revisor, er.comentarios
FROM paquetes_salida p JOIN estado_revision er ON p.run_id = er.run_id AND p.numero_revision = er.numero_revision LEFT JOIN post_linkedin pl ON p.run_id = pl.run_id AND p.numero_revision = pl.numero_revision LEFT JOIN resumen_semanal rs ON p.run_id = rs.run_id AND p.numero_revision = rs.numero_revision WHERE er.estado = 'pendiente';
"""


# =============================================================================
# Infraestructura de conexión / transacción / DDL / migración
# =============================================================================
def _ahora() -> str:
    return datetime.now(UTC).isoformat()


def _sentencias(script: str) -> Iterator[str]:
    """Divide un script SQL en sentencias completas (sin `executescript`, que
    haría COMMIT implícito y rompería la atomicidad de la migración)."""
    acumulado = ""
    for linea in script.splitlines():
        if not acumulado and (not linea.strip() or linea.lstrip().startswith("--")):
            continue
        acumulado += linea + "\n"
        if sqlite3.complete_statement(acumulado):
            yield acumulado
            acumulado = ""


def _ejecutar_ddl(cur: sqlite3.Cursor) -> None:
    for sentencia in _sentencias(DDL_ESQUELETO):
        cur.execute(sentencia)


@contextlib.contextmanager
def _conectar(db_path: str | Path | None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_resolver_db_path(db_path), timeout=DB_TIMEOUT_SEGUNDOS, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _transaccion(db_path: str | Path | None) -> Iterator[sqlite3.Cursor]:
    """Transacción `BEGIN IMMEDIATE`: toma el candado de escritura al inicio
    (respeta `timeout`) en lugar de fallar al "ascender" una lectura."""
    with _conectar(db_path) as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            yield cur
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        else:
            conn.execute("COMMIT;")


def _tabla_existe(cur: sqlite3.Cursor, nombre: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (nombre,)
    ).fetchone() is not None


def _columnas(cur: sqlite3.Cursor, tabla: str) -> list[str]:
    return [f[1] for f in cur.execute(f"PRAGMA table_info({tabla});").fetchall()]


def _normalizar_lotes_procesamiento(cur: sqlite3.Cursor) -> None:
    """Garantiza que `lotes_procesamiento` cuente con la columna `estado_flujo` (spec §8.8)."""
    if not _tabla_existe(cur, "lotes_procesamiento"):
        return
    if "estado_flujo" not in _columnas(cur, "lotes_procesamiento"):
        cur.execute(
            "ALTER TABLE lotes_procesamiento ADD COLUMN estado_flujo TEXT NOT NULL DEFAULT 'iniciado';"
        )


def _normalizar_auditoria_rechazados(cur: sqlite3.Cursor) -> None:
    """Deja `auditoria_rechazados` con las columnas actuales, sea cual sea su origen.

    * `fecha_isolation` -> `fecha_aislamiento`: una versión intermedia del DDL
      tenía el typo en el nombre de la columna mientras los INSERT usaban el
      correcto, así que las bases creadas con ella no admitían ningún rechazo.
    * Añade `origen` ('validador' | 'analisis'), clasificando lo ya guardado.

    Debe ejecutarse ANTES del DDL: este crea un índice sobre `origen`.
    """
    if not _tabla_existe(cur, "auditoria_rechazados"):
        return
    columnas = _columnas(cur, "auditoria_rechazados")
    if "fecha_isolation" in columnas and "fecha_aislamiento" not in columnas:
        cur.execute("ALTER TABLE auditoria_rechazados RENAME COLUMN fecha_isolation TO fecha_aislamiento;")
    if "origen" not in columnas:
        cur.execute("ALTER TABLE auditoria_rechazados ADD COLUMN origen TEXT NOT NULL DEFAULT 'validador';")
        cur.execute(
            "UPDATE auditoria_rechazados SET origen = 'analisis' "
            "WHERE error_detalle LIKE 'ERROR_INTEGRIDAD:%' OR error_detalle LIKE 'ERROR_NEGOCIO_CHECK:%' "
            "OR error_detalle LIKE 'ERROR_DDL_CHECK:%';"
        )


def _migrar_v1_a_v2(cur: sqlite3.Cursor) -> None:
    """Reconstruye las 5 tablas afectadas por la clave compuesta conservando
    los datos. Se ejecuta con FK desactivadas y dentro de una transacción."""
    logger.warning("migracion_esquema_db_iniciada", de=1, a=2)
    for vista in _VISTAS:
        cur.execute(f"DROP VIEW IF EXISTS {vista};")
    hijas = ("mensaje_temas", "mensaje_entidades", "post_linkedin_source_ids", "resumen_semanal_source_ids")
    # legacy_alter_table: el RENAME no debe reescribir las FK de otras tablas.
    cur.execute("PRAGMA legacy_alter_table = ON;")
    for tabla in ("mensajes_analizados", *hijas):
        if _tabla_existe(cur, tabla):
            cur.execute(f"ALTER TABLE {tabla} RENAME TO {tabla}_v1;")
    cur.execute("PRAGMA legacy_alter_table = OFF;")
    _ejecutar_ddl(cur)  # crea las tablas v2 con sus nombres definitivos

    if _tabla_existe(cur, "mensajes_analizados_v1"):
        # Lista explícita: en v2 `run_id` pasó a ser la primera columna, así que
        # un `SELECT *` por posición intercambiaría run_id y source_id.
        cols = ("run_id, source_id, autor, canal, tipo, texto_limpio, sentimiento, categoria_enrutamiento, "
                "regla_aplicada, evidencia_explicita, utilidad_comunitaria, claridad_contexto, puntuacion_total, "
                "motivo_seleccion, requiere_soporte, apto_para_publicacion, fecha_analisis")
        cur.execute(f"INSERT INTO mensajes_analizados ({cols}) SELECT {cols} FROM mensajes_analizados_v1;")
        for tabla, col in (("mensaje_temas", "tema"), ("mensaje_entidades", "entidad")):
            if _tabla_existe(cur, f"{tabla}_v1"):
                cur.execute(
                    f"INSERT INTO {tabla} (id, run_id, source_id, {col}) "
                    f"SELECT h.id, m.run_id, h.source_id, h.{col} "
                    f"FROM {tabla}_v1 h JOIN mensajes_analizados m ON m.source_id = h.source_id;"
                )
    for tabla in ("post_linkedin_source_ids", "resumen_semanal_source_ids"):
        if _tabla_existe(cur, f"{tabla}_v1"):
            cur.execute(
                f"INSERT INTO {tabla} (id, run_id, numero_revision, source_id) "
                f"SELECT id, run_id, numero_revision, source_id FROM {tabla}_v1;"
            )
    for tabla in ("mensajes_analizados", *hijas):
        cur.execute(f"DROP TABLE IF EXISTS {tabla}_v1;")

    huerfanos = cur.execute("PRAGMA foreign_key_check;").fetchall()
    if huerfanos:
        raise RuntimeError(f"La migración dejó {len(huerfanos)} referencias huérfanas; se revierte")
    logger.warning("migracion_esquema_db_completada", a=2)


def inicializar_db(db_path: str | Path | None = None) -> None:
    """Crea el esqueleto de tablas, índices y vistas, y migra una base v1.

    Seguro de ejecutar varias veces y en una base compartida con la API
    (`solicitudes_ingesta`), que esta función no toca.
    """
    ruta = _resolver_db_path(db_path)
    conn = sqlite3.connect(ruta, timeout=DB_TIMEOUT_SEGUNDOS, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = OFF;")  # requisito de la reconstrucción de tablas
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE;")
        try:
            version = cur.execute("PRAGMA user_version;").fetchone()[0]
            _normalizar_lotes_procesamiento(cur)
            _normalizar_auditoria_rechazados(cur)
            if version < VERSION_ESQUEMA_DB and _tabla_existe(cur, "mensajes_analizados"):
                if "run_id" in _columnas(cur, "mensaje_temas"):
                    pass  # ya está en v2 (p. ej. base creada por una versión intermedia)
                else:
                    _migrar_v1_a_v2(cur)
            _ejecutar_ddl(cur)
            _normalizar_lotes_procesamiento(cur)
            cur.execute(f"PRAGMA user_version = {VERSION_ESQUEMA_DB};")
            conn.execute("COMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        logger.info("inicializacion_base_de_datos_exitosa", path=ruta)
    except Exception as e:
        logger.error("error_inicializacion_base_de_datos", error=str(e))
        raise
    finally:
        conn.close()


# =============================================================================
# Helpers a nivel de cursor (se componen en una sola transacción)
# =============================================================================
def _upsert_lote(
    cur: sqlite3.Cursor,
    run_id: str,
    request_id: str,
    origen_comunidad: str,
    periodo_referencia: str,
    total_recibidos: int,
    total_validos: int,
    total_rechazados: int,
    fecha: str,
) -> None:
    """Upsert del encabezado: NO dispara ON DELETE CASCADE sobre los hijos."""
    cur.execute(
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
        (run_id, request_id, origen_comunidad, periodo_referencia,
         total_recibidos, total_validos, total_rechazados, fecha),
    )


def _asegurar_lote_padre(
    cur: sqlite3.Cursor,
    run_id: str,
    request_id: str,
    origen_comunidad: str = "Desconocido",
    periodo_referencia: str = "Sin_Periodo",
    total_recibidos: int = 0,
    total_validos: int = 0,
    total_rechazados: int = 0,
) -> None:
    """Garantiza la fila padre sin pisar una ya registrada con datos completos.
    `registrar_lote_procesamiento` (upsert) corrige después los totales."""
    cur.execute(
        """
        INSERT OR IGNORE INTO lotes_procesamiento (
            run_id, request_id, origen_comunidad, periodo_referencia,
            total_recibidos, total_validos, total_rechazados, fecha_procesamiento
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (run_id, request_id, origen_comunidad, periodo_referencia,
         total_recibidos, total_validos, total_rechazados, _ahora()),
    )


def _insertar_dlq(cur: sqlite3.Cursor, filas: Sequence[tuple[Any, ...]]) -> None:
    if filas:
        cur.executemany(
            """
            INSERT INTO auditoria_rechazados (
                run_id, request_id, mensaje_id, autor, canal,
                error_detalle, texto_crudo, fecha_aislamiento, origen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            filas,
        )


def _insertar_rechazados_validador(
    cur: sqlite3.Cursor, run_id: str, request_id: str, rechazados: Sequence[dict[str, Any]]
) -> int:
    """Reemplaza los rechazos del validador de ese run (idempotente)."""
    cur.execute("DELETE FROM auditoria_rechazados WHERE run_id = ? AND origen = 'validador';", (run_id,))
    filas = [
        (
            run_id,
            request_id,
            r.get("mensaje_id") or r.get("id"),
            r.get("autor", "Anónimo"),
            r.get("canal", "sin_canal"),
            r.get("error_detalle") or r.get("error", "Error desconocido de validación"),
            r.get("texto_crudo", ""),
            r.get("fecha_aislamiento") or _ahora(),
            "validador",
        )
        for r in rechazados
    ]
    _insertar_dlq(cur, filas)
    return len(filas)


def _insertar_resumen_comunidad(cur: sqlite3.Cursor, run_id: str, resumen: Any, fecha_gen: str) -> None:
    """Persiste de forma idempotente `ResumenComunidad` y sus 3 tablas hijas."""
    cur.execute("DELETE FROM resumen_distribucion_sentimiento WHERE run_id = ?;", (run_id,))
    cur.execute("DELETE FROM resumen_temas_principales WHERE run_id = ?;", (run_id,))
    cur.execute("DELETE FROM resumen_alertas_soporte WHERE run_id = ?;", (run_id,))
    cur.execute(
        """
        INSERT INTO resumen_comunidad (
            run_id, total_interacciones_procesadas, registros_validos,
            registros_rechazados, sentimiento_predominante, fecha_generacion
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            total_interacciones_procesadas = excluded.total_interacciones_procesadas,
            registros_validos = excluded.registros_validos,
            registros_rechazados = excluded.registros_rechazados,
            sentimiento_predominante = excluded.sentimiento_predominante,
            fecha_generacion = excluded.fecha_generacion;
        """,
        (run_id, resumen.total_interacciones_procesadas, resumen.registros_validos,
         resumen.registros_rechazados, resumen.sentimiento_predominante, fecha_gen),
    )
    if getattr(resumen, "distribucion_sentimiento", None):
        cur.executemany(
            "INSERT INTO resumen_distribucion_sentimiento (run_id, sentimiento, conteo) VALUES (?, ?, ?);",
            [(run_id, s, int(c)) for s, c in resumen.distribucion_sentimiento.items()],
        )
    if getattr(resumen, "temas_principales", None):
        cur.executemany(
            "INSERT INTO resumen_temas_principales (run_id, tema, orden) VALUES (?, ?, ?);",
            [(run_id, t, i) for i, t in enumerate(resumen.temas_principales, start=1)],
        )
    if getattr(resumen, "alertas_soporte", None):
        cur.executemany(
            "INSERT INTO resumen_alertas_soporte (run_id, alerta) VALUES (?, ?);",
            [(run_id, a) for a in resumen.alertas_soporte],
        )


def _insertar_analisis(
    cur: sqlite3.Cursor, analisis: Any, interacciones_origen: Sequence[Any]
) -> tuple[int, int]:
    run_id, request_id = analisis.run_id, analisis.request_id
    fecha = _ahora()
    origen_por_id = {i.id: i for i in interacciones_origen}

    sanos: list[tuple[Any, ...]] = []
    temas: list[tuple[Any, ...]] = []
    entidades: list[tuple[Any, ...]] = []
    dlq: list[tuple[Any, ...]] = []

    for msg in analisis.mensajes_analizados:
        sid = msg.source_id
        origen = origen_por_id.get(sid)
        if origen is None:
            logger.error("error_integridad_origen", source_id=sid, run_id=run_id)
            dlq.append((run_id, request_id, sid, "Desconocido", "Desconocido",
                        "ERROR_INTEGRIDAD: El ID analizado por la IA no existe en la ingesta original.",
                        f"Motivo IA: {msg.motivo_seleccion}", fecha, "analisis"))
            continue
        if len(msg.motivo_seleccion) < 10:
            logger.error("error_validacion_ddl_motivo", source_id=sid, run_id=run_id)
            dlq.append((run_id, request_id, sid, origen.autor, origen.canal,
                        "ERROR_NEGOCIO_CHECK: 'motivo_seleccion' inferior a 10 caracteres.",
                        origen.texto, fecha, "analisis"))
            continue
        p = msg.puntuacion_relevancia
        sanos.append((
            run_id, sid, origen.autor, origen.canal, origen.tipo, origen.texto,
            msg.sentimiento, msg.categoria_enrutamiento, msg.regla_aplicada,
            p.evidencia_explicita, p.utilidad_comunitaria, p.claridad_contexto, p.total,
            msg.motivo_seleccion, 1 if msg.requiere_soporte else 0,
            1 if msg.apto_para_publicacion else 0, fecha,
        ))
        temas.extend((run_id, sid, t) for t in msg.temas)
        entidades.extend((run_id, sid, e) for e in msg.entidades_relevantes)

    plataforma = (
        interacciones_origen[0].metadata_origen.plataforma
        if interacciones_origen and hasattr(interacciones_origen[0], "metadata_origen")
        else "Desconocido"
    )
    # El análisis no cuenta como "rechazado" del validador (recomendación 10).
    _asegurar_lote_padre(cur, run_id, request_id, origen_comunidad=plataforma,
                         total_recibidos=len(interacciones_origen),
                         total_validos=len(interacciones_origen))

    cur.execute("DELETE FROM auditoria_rechazados WHERE run_id = ? AND origen = 'analisis';", (run_id,))
    cur.executemany("DELETE FROM mensaje_temas WHERE run_id = ? AND source_id = ?;",
                    [(r[0], r[1]) for r in sanos])
    cur.executemany("DELETE FROM mensaje_entidades WHERE run_id = ? AND source_id = ?;",
                    [(r[0], r[1]) for r in sanos])
    if sanos:
        cur.executemany(
            """
            INSERT INTO mensajes_analizados (
                run_id, source_id, autor, canal, tipo, texto_limpio, sentimiento,
                categoria_enrutamiento, regla_aplicada, evidencia_explicita,
                utilidad_comunitaria, claridad_contexto, puntuacion_total,
                motivo_seleccion, requiere_soporte, apto_para_publicacion, fecha_analisis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, source_id) DO UPDATE SET
                autor = excluded.autor, canal = excluded.canal, tipo = excluded.tipo,
                texto_limpio = excluded.texto_limpio, sentimiento = excluded.sentimiento,
                categoria_enrutamiento = excluded.categoria_enrutamiento,
                regla_aplicada = excluded.regla_aplicada,
                evidencia_explicita = excluded.evidencia_explicita,
                utilidad_comunitaria = excluded.utilidad_comunitaria,
                claridad_contexto = excluded.claridad_contexto,
                puntuacion_total = excluded.puntuacion_total,
                motivo_seleccion = excluded.motivo_seleccion,
                requiere_soporte = excluded.requiere_soporte,
                apto_para_publicacion = excluded.apto_para_publicacion,
                fecha_analisis = excluded.fecha_analisis;
            """,
            sanos,
        )
    if temas:
        cur.executemany("INSERT INTO mensaje_temas (run_id, source_id, tema) VALUES (?, ?, ?);", temas)
    if entidades:
        cur.executemany("INSERT INTO mensaje_entidades (run_id, source_id, entidad) VALUES (?, ?, ?);", entidades)
    if getattr(analisis, "resumen_comunidad", None) is not None:
        _insertar_resumen_comunidad(cur, run_id, analisis.resumen_comunidad, fecha)
    _insertar_dlq(cur, dlq)
    logger.info("insercion_analitica_bloque_con_dlq_exitosa", run_id=run_id,
                registros_sanos=len(sanos), registros_desviados_dlq=len(dlq))
    return len(sanos), len(dlq)


def _iso(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = valor.isoformat() if hasattr(valor, "isoformat") else str(valor)
    # Misma forma que la serialización JSON de Pydantic (UTC -> "Z"): el JSON
    # reconstruido debe ser idéntico, byte a byte, al que se subió a OCI.
    return texto[:-6] + "Z" if texto.endswith("+00:00") else texto


def _insertar_paquete(
    cur: sqlite3.Cursor,
    paquete: Any,
    origen_comunidad: str | None = None,
    periodo_referencia: str | None = None,
) -> int:
    run_id, request_id = paquete.run_id, paquete.request_id
    rev = paquete.revision.numero_revision
    fecha_gen = _iso(paquete.fecha_generacion) or _ahora()
    res = paquete.analisis_resumido

    _asegurar_lote_padre(
        cur,
        run_id,
        request_id,
        origen_comunidad or "Desconocido",
        periodo_referencia or "Sin_Periodo",
        total_recibidos=res.total_interacciones_procesadas,
        total_validos=res.registros_validos,
        total_rechazados=res.registros_rechazados,
    )
    _insertar_resumen_comunidad(cur, run_id, res, fecha_gen)

    # Reejecución idempotente del mismo (run_id, revisión): el CASCADE limpia hijas.
    cur.execute("DELETE FROM paquetes_salida WHERE run_id = ? AND numero_revision = ?;", (run_id, rev))
    cur.execute(
        "INSERT INTO paquetes_salida (run_id, numero_revision, request_id, schema_version, fecha_generacion) "
        "VALUES (?, ?, ?, ?, ?);",
        (run_id, rev, request_id, paquete.schema_version, fecha_gen),
    )
    m = paquete.metadatos_ejecucion
    cur.execute(
        "INSERT INTO metadatos_ejecucion_paquete (run_id, numero_revision, modelo, prompt_version, latencia_ms, tokens_totales) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (run_id, rev, m.modelo, m.prompt_version, m.latencia_ms, m.tokens_totales),
    )
    pl = paquete.activos.post_linkedin
    cur.execute(
        "INSERT INTO post_linkedin (run_id, numero_revision, titulo, canal_recomendado, cuerpo) VALUES (?, ?, ?, ?, ?);",
        (run_id, rev, pl.titulo, pl.canal_recomendado, pl.cuerpo),
    )
    cur.executemany("INSERT INTO post_linkedin_hashtags (run_id, numero_revision, hashtag) VALUES (?, ?, ?);",
                    [(run_id, rev, h) for h in pl.hashtags])
    cur.executemany("INSERT INTO post_linkedin_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, ?);",
                    [(run_id, rev, s) for s in pl.source_ids])
    rs = paquete.activos.resumen_semanal
    cur.execute(
        "INSERT INTO resumen_semanal (run_id, numero_revision, seccion, titular, resumen) VALUES (?, ?, ?, ?, ?);",
        (run_id, rev, rs.seccion, rs.titular, rs.resumen),
    )
    cur.executemany("INSERT INTO resumen_semanal_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, ?);",
                    [(run_id, rev, s) for s in rs.source_ids])
    r = paquete.revision
    cur.execute(
        "INSERT INTO estado_revision (run_id, numero_revision, estado, revisor, fecha_decision, comentarios) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (run_id, rev, r.estado, r.revisor, _iso(r.fecha_decision), r.comentarios),
    )
    o = paquete.almacenamiento_oci
    cur.execute(
        "INSERT INTO evidencia_almacenamiento_oci (run_id, numero_revision, bucket, ruta_objeto, status_almacenamiento, comprobacion_lectura) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (run_id, rev, o.bucket, o.ruta_objeto, o.status_almacenamiento, 1 if o.comprobacion_lectura else 0),
    )
    logger.info("paquete_salida_registrado", run_id=run_id, numero_revision=rev)
    return rev


def _insertar_telemetria(
    cur: sqlite3.Cursor, run_id: str, request_id: str, registros: Sequence[Any]
) -> int:
    """Reemplaza la telemetría del run (idempotente). Acepta objetos con
    `proveedor`, `modelo`, `latencia_ms`, `tokens_entrada`, `tokens_salida`,
    `tokens_cacheados` y `tokens_totales` (p. ej. `TelemetriaAnalisis`)."""
    cur.execute("DELETE FROM telemetria_llamadas_llm WHERE run_id = ?;", (run_id,))
    fecha = _ahora()
    filas = [
        (run_id, request_id, getattr(t, "source_id", None), t.proveedor, t.modelo, int(t.latencia_ms),
         int(t.tokens_entrada), int(t.tokens_salida), int(t.tokens_totales),
         int(getattr(t, "tokens_cacheados", 0) or 0), fecha)
        for t in registros
    ]
    if filas:
        cur.executemany(
            "INSERT INTO telemetria_llamadas_llm (run_id, request_id, source_id, proveedor, modelo, latencia_ms, "
            "prompt_tokens, completion_tokens, tokens_totales, tokens_cacheados, fecha_evento) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            filas,
        )
    return len(filas)


# =============================================================================
# API pública
# =============================================================================
@dataclass(frozen=True)
class ResultadoPersistencia:
    mensajes_sanos: int = 0
    mensajes_dlq: int = 0
    rechazados_validador: int = 0
    revision_paquete: int | None = None


def persistir_resultado(
    *,
    run_id: str,
    request_id: str,
    origen_comunidad: str,
    periodo_referencia: str,
    total_recibidos: int,
    total_validos: int,
    rechazados_validador: Sequence[dict[str, Any]] = (),
    analisis_cognitivo: Any = None,
    interacciones_origen: Sequence[Any] = (),
    paquete: Any = None,
    telemetria: Sequence[Any] = (),
    db_path: str | Path | None = None,
) -> ResultadoPersistencia:
    """Persiste TODO el resultado de un lote en UNA transacción atómica.

    Sirve a los cuatro estados terminales del orquestador:
    lote vacío (solo lote + rechazos), sin contenido publicable (+ análisis y
    telemetría), completado (+ paquete). Si algo falla, no queda nada a medias.
    Es idempotente por `run_id`: reejecutarla reemplaza el resultado anterior.
    """
    if analisis_cognitivo is not None and (
        analisis_cognitivo.run_id != run_id or analisis_cognitivo.request_id != request_id
    ):
        raise ValueError("El análisis cognitivo pertenece a otro run_id/request_id")
    if paquete is not None and paquete.run_id != run_id:
        raise ValueError("El paquete de salida pertenece a otro run_id")
    try:
        with _transaccion(db_path) as cur:
            _upsert_lote(cur, run_id, request_id, origen_comunidad, periodo_referencia,
                         total_recibidos, total_validos, len(rechazados_validador), _ahora())
            n_rech = _insertar_rechazados_validador(cur, run_id, request_id, rechazados_validador)
            sanos = dlq = 0
            if analisis_cognitivo is not None:
                sanos, dlq = _insertar_analisis(cur, analisis_cognitivo, interacciones_origen)
            rev = None
            if paquete is not None:
                rev = _insertar_paquete(cur, paquete, origen_comunidad, periodo_referencia)
            if telemetria:
                _insertar_telemetria(cur, run_id, request_id, telemetria)
        logger.info("resultado_persistido", run_id=run_id, mensajes=sanos, dlq=dlq, rechazados=n_rech)
        return ResultadoPersistencia(sanos, dlq, n_rech, rev)
    except Exception as e:
        logger.error("error_persistir_resultado", run_id=run_id, error=str(e))
        raise


def registrar_lote_procesamiento(
    run_id: str,
    request_id: str,
    origen_comunidad: str,
    periodo_referencia: str,
    total_recibidos: int,
    total_validos: int,
    total_rechazados: int,
    fecha_procesamiento: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Registra o actualiza (upsert) el encabezado del lote."""
    with _transaccion(db_path) as cur:
        _upsert_lote(cur, run_id, request_id, origen_comunidad, periodo_referencia,
                     total_recibidos, total_validos, total_rechazados,
                     fecha_procesamiento or _ahora())
    logger.info("lote_procesamiento_registrado", run_id=run_id, request_id=request_id)


def registrar_analisis_bloque(
    analisis_cognitivo: Any,
    interacciones_origen: list[Any],
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    """Inserta en bloque y de forma atómica los resultados analíticos de la IA.
    Devuelve (registros_sanos, registros_a_dlq)."""
    with _transaccion(db_path) as cur:
        return _insertar_analisis(cur, analisis_cognitivo, interacciones_origen)


def registrar_rechazados_validador(
    rechazados: list[dict[str, Any]],
    db_path: str | Path | None = None,
    *,
    run_id: str | None = None,
) -> int:
    """Persiste los descartes del validador, de forma atómica e idempotente.

    Cada rechazo necesita un `run_id`: sin él no hay forma de purgar los
    rechazos de una ejecución previa y un reintento duplicaría las filas. Si
    el validador se invocó sin `run_id` (su parámetro es opcional y entonces
    cada diccionario trae `run_id=None`), se puede indicar aquí con `run_id=`.
    """
    if not rechazados:
        return 0
    por_run: dict[str, list[dict[str, Any]]] = {}
    for r in rechazados:
        rid = r.get("run_id") or run_id
        if not rid:
            raise ValueError(
                "Cada rechazo del validador debe incluir 'run_id' (o pasarlo con run_id=): idempotencia"
            )
        por_run.setdefault(rid, []).append({**r, "run_id": rid})
    total = 0
    with _transaccion(db_path) as cur:
        for rid, items in por_run.items():
            request_id = items[0].get("request_id", "SIN_REQ_ID")
            _asegurar_lote_padre(cur, rid, request_id, total_recibidos=len(items),
                                 total_rechazados=len(items))
            total += _insertar_rechazados_validador(cur, rid, request_id, items)
    logger.info("persistencia_rechazados_validador_exitosa", total=total)
    return total


def registrar_paquete_salida(
    paquete: Any,
    origen_comunidad: str = "Desconocido",
    periodo_referencia: str = "Sin_Periodo",
    db_path: str | Path | None = None,
) -> None:
    """Persiste de forma atómica e idempotente el Contrato 3 (`PaqueteSalida`)."""
    with _transaccion(db_path) as cur:
        _insertar_paquete(cur, paquete, origen_comunidad, periodo_referencia)


def registrar_telemetria(
    run_id: str,
    request_id: str,
    registros: Sequence[Any],
    db_path: str | Path | None = None,
) -> int:
    """Reemplaza la telemetría de LLM del run (una fila por etapa/llamada)."""
    with _transaccion(db_path) as cur:
        return _insertar_telemetria(cur, run_id, request_id, registros)


def reconstruir_json_paquete_salida(
    run_id: str,
    numero_revision: int,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reconstruye el JSON del Contrato 3 desde las tablas relacionales.

    Es determinista (todo con ORDER BY), no inventa valores y se valida contra
    `PaqueteSalida` antes de devolverse: si la base quedó inconsistente se
    lanza `ValueError` en lugar de producir un JSON que no cumple el contrato.
    """
    with _conectar(db_path) as conn:
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT
                p.schema_version, p.request_id, p.fecha_generacion,
                mep.modelo, mep.prompt_version, mep.latencia_ms, mep.tokens_totales,
                rc.total_interacciones_procesadas, rc.registros_validos, rc.registros_rechazados, rc.sentimiento_predominante,
                pl.titulo, pl.cuerpo, pl.canal_recomendado,
                rs.seccion, rs.titular, rs.resumen,
                er.estado, er.revisor, er.fecha_decision, er.comentarios,
                eo.bucket, eo.ruta_objeto, eo.status_almacenamiento, eo.comprobacion_lectura
            FROM paquetes_salida p
            JOIN resumen_comunidad rc ON p.run_id = rc.run_id
            LEFT JOIN metadatos_ejecucion_paquete mep ON p.run_id = mep.run_id AND p.numero_revision = mep.numero_revision
            LEFT JOIN post_linkedin pl ON p.run_id = pl.run_id AND p.numero_revision = pl.numero_revision
            LEFT JOIN resumen_semanal rs ON p.run_id = rs.run_id AND p.numero_revision = rs.numero_revision
            LEFT JOIN estado_revision er ON p.run_id = er.run_id AND p.numero_revision = er.numero_revision
            LEFT JOIN evidencia_almacenamiento_oci eo ON p.run_id = eo.run_id AND p.numero_revision = eo.numero_revision
            WHERE p.run_id = ? AND p.numero_revision = ?;
            """,
            (run_id, numero_revision),
        ).fetchone()
        if not row:
            raise ValueError(
                f"No se encontró el paquete de salida para run_id='{run_id}' y revisión={numero_revision}"
            )
        (schema_version, request_id, fecha_gen, modelo, prompt_version, latencia_ms, tokens_totales,
         total_int, reg_validos, reg_rechazados, sent_pred, li_titulo, li_cuerpo, li_canal,
         sem_sec, sem_tit, sem_res, rev_estado, rev_revisor, rev_fecha, rev_comentarios,
         oci_buck, oci_ruta, oci_stat, oci_lectura) = row

        def col(sql: str, *params: Any) -> list[Any]:
            return [r[0] for r in cur.execute(sql, params).fetchall()]

        dist_sent = dict(cur.execute(
            "SELECT sentimiento, conteo FROM resumen_distribucion_sentimiento WHERE run_id = ? ORDER BY sentimiento;",
            (run_id,)).fetchall())
        temas = col("SELECT tema FROM resumen_temas_principales WHERE run_id = ? ORDER BY orden;", run_id)
        alertas = col("SELECT alerta FROM resumen_alertas_soporte WHERE run_id = ? ORDER BY id;", run_id)
        hashtags = col("SELECT hashtag FROM post_linkedin_hashtags WHERE run_id = ? AND numero_revision = ? ORDER BY id;",
                       run_id, numero_revision)
        li_ids = col("SELECT source_id FROM post_linkedin_source_ids WHERE run_id = ? AND numero_revision = ? ORDER BY id;",
                     run_id, numero_revision)
        rs_ids = col("SELECT source_id FROM resumen_semanal_source_ids WHERE run_id = ? AND numero_revision = ? ORDER BY id;",
                     run_id, numero_revision)

        casos = []
        for sid, sent, cat, sop, mot in cur.execute(
            "SELECT source_id, sentimiento, categoria_enrutamiento, requiere_soporte, motivo_seleccion "
            "FROM mensajes_analizados WHERE run_id = ? AND apto_para_publicacion = 0 ORDER BY source_id;",
            (run_id,),
        ).fetchall():
            casos.append({
                "source_id": sid, "sentimiento": sent, "categoria_enrutamiento": cat,
                "temas": col("SELECT tema FROM mensaje_temas WHERE run_id = ? AND source_id = ? ORDER BY id;",
                             run_id, sid),
                "requiere_soporte": bool(sop), "motivo_seleccion": mot,
            })

    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "request_id": request_id,
        "fecha_generacion": fecha_gen,
        "metadatos_ejecucion": {
            "modelo": modelo, "prompt_version": prompt_version,
            "latencia_ms": latencia_ms, "tokens_totales": tokens_totales,
        },
        "analisis_resumido": {
            "total_interacciones_procesadas": total_int,
            "registros_validos": reg_validos,
            "registros_rechazados": reg_rechazados,
            "sentimiento_predominante": sent_pred,
            "distribucion_sentimiento": dist_sent,
            "temas_principales": temas,
            "alertas_soporte": alertas,
        },
        "activos": {
            "post_linkedin": {
                "titulo": li_titulo, "canal_recomendado": li_canal, "cuerpo": li_cuerpo,
                "hashtags": hashtags, "source_ids": li_ids,
            },
            "resumen_semanal": {"seccion": sem_sec, "titular": sem_tit, "resumen": sem_res, "source_ids": rs_ids},
            "casos_no_publicables": casos,
        },
        "revision": {
            "numero_revision": numero_revision, "estado": rev_estado, "revisor": rev_revisor,
            "fecha_decision": rev_fecha, "comentarios": rev_comentarios,
        },
        "almacenamiento_oci": {
            "bucket": oci_buck, "ruta_objeto": oci_ruta, "status_almacenamiento": oci_stat,
            "comprobacion_lectura": bool(oci_lectura),
        },
    }
    try:
        PaqueteSalida.model_validate(payload)
    except Exception as exc:
        logger.error("error_reconstruccion_json_memoria", run_id=run_id, revision=numero_revision,
                     error=str(exc)[:500])
        raise ValueError(
            f"El paquete reconstruido de run_id='{run_id}' revisión={numero_revision} "
            "no cumple el Contrato 3; la base está inconsistente"
        ) from exc
    return payload


def sincronizar_pendientes_oci(
    storage_client: Any,
    db_path: str | Path | None = None,
) -> tuple[int, int]:
    """Reintenta la subida a OCI de los paquetes en 'guardado_error'.

    Devuelve (sincronizaciones_exitosas, fallidas_recurrentes). Un paquete cuya
    reconstrucción no cumple el contrato cuenta como fallido y NO se sube.
    """
    ruta = _resolver_db_path(db_path)
    with contextlib.closing(sqlite3.connect(ruta, timeout=DB_TIMEOUT_SEGUNDOS)) as conn:
        pendientes = conn.execute(
            """
            SELECT e.run_id, e.numero_revision, l.periodo_referencia, l.request_id
            FROM evidencia_almacenamiento_oci e
            JOIN lotes_procesamiento l ON e.run_id = l.run_id
            WHERE e.status_almacenamiento = 'guardado_error'
            ORDER BY e.run_id, e.numero_revision;
            """
        ).fetchall()

    if not pendientes:
        logger.info("sincronizacion_oci_sin_pendientes", detalle="La cola de reintentos está vacía.")
        return 0, 0

    logger.info("sincronizacion_oci_iniciada", total_lote=len(pendientes))
    exitosos = fallidos = 0
    for run_id, revision, periodo, request_id in pendientes:
        try:
            payload = reconstruir_json_paquete_salida(run_id, revision, db_path=ruta)
            # El orquestador sube el paquete con la evidencia en su estado inicial
            # ("no_iniciado", sin lectura): el cliente de OCI exige que ese campo no
            # llegue precompuesto. El reintento debe subir EXACTAMENTE esos bytes; si
            # no, un objeto que sí llegó a OCI (falló solo la lectura de verificación)
            # dispara un 412 con hash distinto y queda como colisión permanente.
            payload["almacenamiento_oci"]["status_almacenamiento"] = "no_iniciado"
            payload["almacenamiento_oci"]["comprobacion_lectura"] = False
            resultado = storage_client.upload_package(
                package_payload=payload, periodo_referencia=periodo, run_id=run_id,
                numero_revision=revision, request_id=request_id,
            )
            if resultado.status_almacenamiento == "guardado_con_exito" and resultado.comprobacion_lectura:
                with contextlib.closing(sqlite3.connect(ruta, timeout=DB_TIMEOUT_SEGUNDOS)) as c2:
                    c2.execute(
                        "UPDATE evidencia_almacenamiento_oci SET status_almacenamiento = 'guardado_con_exito', "
                        "comprobacion_lectura = 1 WHERE run_id = ? AND numero_revision = ?;",
                        (run_id, revision),
                    )
                    c2.commit()
                exitosos += 1
                logger.info("reintento_sincronizacion_oci_exitoso", run_id=run_id, revision=revision)
            else:
                fallidos += 1
                logger.warning("reintento_sincronizacion_oci_fallido_recurrente", run_id=run_id,
                               error=resultado.mensaje_error)
        except Exception as e:  # noqa: BLE001 - un paquete roto no debe frenar a los demás
            fallidos += 1
            logger.error("error_grave_iteracion_sincronizador", run_id=run_id, error=str(e))
    logger.info("sincronizacion_oci_finalizada", exitosos=exitosos, fallidos=fallidos)
    return exitosos, fallidos


# =============================================================================
# Fachada para el orquestador (la spec v3.1 importa `LocalDatabaseManager`)
# =============================================================================
class LocalDatabaseManager:
    """Envoltorio delgado sobre las funciones del módulo con una ruta fija.

    Crea/migra la base al construirse (idempotente y seguro con la API, que
    comparte el archivo). La ruta sigue la misma precedencia que el resto del
    sistema: argumento > `COMMUNITYLAB_DATA_DIR` > `SQLITE_DB_PATH`.
    Expone todos los métodos requeridos por `orchestration/pipeline_runner.py`.
    """

    def __init__(self, db_path: str | Path | None = None, *, inicializar: bool = True) -> None:
        self.db_path = _resolver_db_path(db_path)
        if inicializar:
            inicializar_db(self.db_path)

    def registrar_lote(
        self,
        run_id: str,
        request_id: str,
        origen_comunidad: str,
        periodo_referencia: str,
        total_recibidos: int,
        total_validos: int,
        total_rechazados: int,
        rechazados: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Registra o actualiza el lote y, si se provee `rechazados`, reemplaza
        los rechazos del validador dentro de la misma transacción atómica."""
        with _transaccion(self.db_path) as cur:
            _upsert_lote(
                cur,
                run_id,
                request_id,
                origen_comunidad,
                periodo_referencia,
                total_recibidos,
                total_validos,
                total_rechazados,
                _ahora(),
            )
            if rechazados is not None:
                _insertar_rechazados_validador(cur, run_id, request_id, rechazados)

    def registrar_lote_y_analisis(
        self,
        analisis: Any,
        interacciones_validas: Sequence[Any],
        origen_comunidad: str,
        periodo_referencia: str,
        total_recibidos: int,
        total_rechazados: int,
    ) -> tuple[int, int]:
        """Actualiza el encabezado del lote e inserta el análisis cognitivo y
        `ResumenComunidad` dentro de un único bloque transaccional atómico."""
        with _transaccion(self.db_path) as cur:
            _upsert_lote(
                cur,
                analisis.run_id,
                analisis.request_id,
                origen_comunidad,
                periodo_referencia,
                total_recibidos,
                len(interacciones_validas),
                total_rechazados,
                _ahora(),
            )
            return _insertar_analisis(cur, analisis, interacciones_validas)

    def registrar_paquete(
        self,
        paquete: Any,
        origen_comunidad: str | None = None,
        periodo_referencia: str | None = None,
    ) -> int:
        """Persiste el Contrato 3 (`PaqueteSalida`) y su evidencia de OCI."""
        with _transaccion(self.db_path) as cur:
            return _insertar_paquete(cur, paquete, origen_comunidad, periodo_referencia)

    def registrar_telemetria(self, run_id: str, request_id: str, registros: Sequence[Any]) -> int:
        """Reemplaza la telemetría de LLM del run (una fila por etapa). Idempotente:
        pasar SIEMPRE la lista completa del run, no solo la etapa nueva."""
        return registrar_telemetria(run_id, request_id, registros, db_path=self.db_path)

    def leer_rechazados(self, run_id: str) -> list[dict[str, Any]]:
        """Recupera los rechazos del validador asociados a `run_id` para que
        `_resultado_fallido` en `pipeline_runner.py` pueda reportarlos."""
        with _conectar(self.db_path) as conn:
            filas = conn.execute(
                """
                SELECT mensaje_id, autor, canal, error_detalle, request_id, texto_crudo, fecha_aislamiento
                FROM auditoria_rechazados
                WHERE run_id = ? AND origen = 'validador'
                ORDER BY id;
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "id": f[0],
                "mensaje_id": f[0],
                "autor": f[1],
                "canal": f[2],
                "error": f[3],
                "error_detalle": f[3],
                "request_id": f[4],
                "texto_crudo": f[5],
                "fecha_aislamiento": f[6],
                "run_id": run_id,
            }
            for f in filas
        ]

    def actualizar_estado_flujo(self, run_id: str, estado_flujo: str) -> bool:
        """Registra el estado terminal del flujo en `lotes_procesamiento` (spec §8.8)."""
        with _transaccion(self.db_path) as cur:
            cur.execute(
                "UPDATE lotes_procesamiento SET estado_flujo = ? WHERE run_id = ?;",
                (estado_flujo, run_id),
            )
            return cur.rowcount == 1

    def persistir_resultado(self, **kwargs: Any) -> ResultadoPersistencia:
        return persistir_resultado(**kwargs, db_path=self.db_path)

    def reconstruir_json_paquete_salida(self, run_id: str, numero_revision: int) -> dict[str, Any]:
        return reconstruir_json_paquete_salida(run_id, numero_revision, db_path=self.db_path)

    def sincronizar_pendientes_oci(self, storage_client: Any) -> tuple[int, int]:
        return sincronizar_pendientes_oci(storage_client, db_path=self.db_path)