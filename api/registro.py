"""Registro de idempotencia de la ingesta (SQLite).

Tabla propia de la API, `solicitudes_ingesta`: la escribe la API al recibir
y NO sustituye a `lotes_procesamiento` (que escribe el orquestador al
terminar y no guarda la huella del cuerpo).

Garantías
---------
* Alta atómica: la comprobación y el INSERT ocurren dentro de
  `BEGIN IMMEDIATE`, así que dos peticiones simultáneas con el mismo
  `request_id` no pueden crear dos `run_id`.
* Mismo `request_id` + misma huella  -> se reutiliza el `run_id` existente.
* Mismo `request_id` + otra huella   -> `ConflictoIdempotencia` (HTTP 409).
* Seguridad ante caídas: el alta nace en estado `recibiendo` y pasa a
  `en_cola` solo cuando el archivo ya está en `data/raw/`. Si el proceso cae
  entre ambos pasos, un reintento tras la ventana de gracia "retoma" la
  recepción (reescribe el archivo con el mismo `run_id`) en vez de devolver
  un `run_id` sin archivo.

Estados que el runner puede escribir con `actualizar_estado`:
`procesando` y los de `EstadoFlujoFinal` de la spec del orquestador.
El runner debe ser idempotente por `run_id` (PK de `lotes_procesamiento`).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

ESTADOS = (
    "recibiendo",
    "en_cola",
    "procesando",
    "completado",
    "sin_registros_validos",
    "sin_contenido_publicable",
    "fallido",
)

_DDL = f"""
CREATE TABLE IF NOT EXISTS solicitudes_ingesta (
    request_id     TEXT PRIMARY KEY,
    hash_sha256    TEXT NOT NULL CHECK (length(hash_sha256) = 64),
    run_id         TEXT NOT NULL UNIQUE,
    estado         TEXT NOT NULL CHECK (estado IN ({", ".join(repr(e) for e in ESTADOS)})),
    recibido_en    TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);
"""

Accion = Literal["nuevo", "repetido", "retomar", "en_curso"]


class ConflictoIdempotencia(Exception):
    """`request_id` ya registrado con una huella de contenido distinta."""

    def __init__(self, request_id: str) -> None:
        super().__init__("request_id ya registrado con contenido distinto")
        self.request_id = request_id


@dataclass(frozen=True)
class ResultadoRegistro:
    """Qué debe hacer la capa HTTP tras consultar el registro.

    nuevo     -> escribir el archivo y llamar a `marcar_en_cola`.
    retomar   -> igual que `nuevo`, con el `run_id` ya existente
                 (la recepción anterior murió antes de dejar el archivo).
    repetido  -> duplicado idéntico ya encolado: responder sin tocar disco.
    en_curso  -> otra petición idéntica está recibiendo ahora mismo.
    """

    accion: Accion
    run_id: str
    estado: str


@dataclass(frozen=True)
class SolicitudIngesta:
    request_id: str
    hash_sha256: str
    run_id: str
    estado: str
    recibido_en: str
    actualizado_en: str


def generar_run_id(ahora: datetime) -> str:
    """`run-YYYYMMDDTHHMMSSZ-<12 hex>`: cumple `^[A-Za-z0-9._-]+$` (ruta OCI)."""
    return f"run-{ahora:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


def _iso(dt: datetime) -> str:
    # Formato fijo y en UTC: las cadenas se pueden comparar en SQL.
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


class RegistroIdempotencia:
    def __init__(
        self,
        db_path: Path,
        gracia_recepcion_s: float = 30.0,
        reloj: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db_path = Path(db_path)
        self._gracia_s = gracia_recepcion_s
        self._reloj = reloj

    # -- infraestructura ----------------------------------------------------

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        # Una conexión por operación: segura entre hilos y sin estado
        # compartido. isolation_level=None -> las transacciones son explícitas.
        con = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout = 5000")
            yield con
        finally:
            con.close()

    def inicializar(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conexion() as con:
            # WAL permite lecturas (Streamlit, runner) durante las escrituras.
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(_DDL)

    def comprobar(self) -> None:
        """Lanza si la base no responde (usado por /health)."""
        with self._conexion() as con:
            con.execute("SELECT 1").fetchone()

    # -- alta idempotente ---------------------------------------------------

    def registrar(self, request_id: str, hash_sha256: str) -> ResultadoRegistro:
        ahora = self._reloj()
        with self._conexion() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                fila = con.execute(
                    "SELECT hash_sha256, run_id, estado, actualizado_en "
                    "FROM solicitudes_ingesta WHERE request_id = ?",
                    (request_id,),
                ).fetchone()

                if fila is None:
                    run_id = generar_run_id(ahora)
                    con.execute(
                        "INSERT INTO solicitudes_ingesta "
                        "(request_id, hash_sha256, run_id, estado, recibido_en, actualizado_en) "
                        "VALUES (?, ?, ?, 'recibiendo', ?, ?)",
                        (request_id, hash_sha256, run_id, _iso(ahora), _iso(ahora)),
                    )
                    con.execute("COMMIT")
                    return ResultadoRegistro("nuevo", run_id, "recibiendo")

                if fila["hash_sha256"] != hash_sha256:
                    con.execute("ROLLBACK")
                    raise ConflictoIdempotencia(request_id)

                if fila["estado"] != "recibiendo":
                    con.execute("ROLLBACK")
                    return ResultadoRegistro("repetido", fila["run_id"], fila["estado"])

                # Recepción previa sin terminar: solo se retoma si ya venció
                # la gracia, y de forma compare-and-set para que dos
                # reintentos simultáneos no retomen a la vez.
                limite = _iso(ahora - timedelta(seconds=self._gracia_s))
                cur = con.execute(
                    "UPDATE solicitudes_ingesta SET actualizado_en = ? "
                    "WHERE request_id = ? AND estado = 'recibiendo' AND actualizado_en <= ?",
                    (_iso(ahora), request_id, limite),
                )
                con.execute("COMMIT")
                accion: Accion = "retomar" if cur.rowcount == 1 else "en_curso"
                return ResultadoRegistro(accion, fila["run_id"], "recibiendo")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise

    # -- transiciones -------------------------------------------------------

    def marcar_en_cola(self, run_id: str) -> None:
        self.actualizar_estado(run_id, "en_cola", solo_desde=("recibiendo",))

    def actualizar_estado(
        self, run_id: str, estado: str, solo_desde: tuple[str, ...] | None = None
    ) -> bool:
        """Cambia el estado de un run. Devuelve True si hubo cambio.

        `solo_desde` evita pisar un estado más avanzado (p. ej. que un
        `marcar_en_cola` tardío degrade un `procesando`).
        """
        if estado not in ESTADOS:
            raise ValueError(f"estado desconocido: {estado!r}")
        sql = "UPDATE solicitudes_ingesta SET estado = ?, actualizado_en = ? WHERE run_id = ?"
        params: list[object] = [estado, _iso(self._reloj()), run_id]
        if solo_desde:
            sql += f" AND estado IN ({', '.join('?' * len(solo_desde))})"
            params.extend(solo_desde)
        with self._conexion() as con:
            return con.execute(sql, params).rowcount == 1

    def descartar_recepcion(self, run_id: str) -> None:
        """Borra una recepción fallida (no llegó a dejar archivo) para que
        el cliente pueda reintentar desde cero. Solo afecta a `recibiendo`."""
        with self._conexion() as con:
            con.execute(
                "DELETE FROM solicitudes_ingesta WHERE run_id = ? AND estado = 'recibiendo'",
                (run_id,),
            )

    # -- consulta -----------------------------------------------------------

    def obtener_por_run_id(self, run_id: str) -> SolicitudIngesta | None:
        with self._conexion() as con:
            fila = con.execute(
                "SELECT request_id, hash_sha256, run_id, estado, recibido_en, actualizado_en "
                "FROM solicitudes_ingesta WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return SolicitudIngesta(**dict(fila)) if fila else None
