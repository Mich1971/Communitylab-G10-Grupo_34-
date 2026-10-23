"""Worker local: consume `data/raw/` y ejecuta el pipeline.

Cierra el hueco entre la API (que solo deposita lotes y responde 202) y el
orquestador (`CommunityLabPipeline.ejecutar_desde_archivo`).

Ciclo de vida de un archivo
---------------------------
    data/raw/{run_id}.json         depositado por la API (os.replace atómico)
      -> data/processing/{run_id}.json   reclamado con os.replace (atómico)
      -> data/processed/{run_id}.json    estado terminal de negocio
      -> data/failed/{run_id}.json       estado `fallido` (para inspección)

* Los `temp_*` de la API jamás se tocan: solo se reclaman nombres que
  cumplen `run-YYYYMMDDTHHMMSSZ-<12 hex>.json`.
* Nota sobre lotes manuales (p. ej. `lote_prueba_01.json`): para procesarlos
  desde CLI o pruebas directas se invoca `CommunityLabPipeline.ejecutar_desde_archivo(...)`;
  si se encolan desde Streamlit para que los tome este worker, deben depositarse
  mediante `RegistroIdempotencia.registrar()` + `escribir_lote_atomico()`.
* Soporte multiplataforma (Riesgo 6): usa `fcntl.flock` en Linux/macOS/WSL2 y
  `msvcrt.locking` en Windows nativo para que todo el equipo pueda ejecutarlo.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - entorno Windows nativo
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - entorno POSIX
    msvcrt = None  # type: ignore[assignment]

import structlog
from pydantic import ValidationError

from api.registro import ESTADOS_TERMINALES, RegistroIdempotencia

log = structlog.get_logger("orchestration.worker")

_NOMBRE_RUN = re.compile(r"^(run-\d{8}T\d{6}Z-[0-9a-f]{12})\.json$")
_TERMINALES = ESTADOS_TERMINALES

# Intentos de ejecución por run antes de darlo por fallido. Un lote que mata el
# proceso en cada intento (OOM, segfault) se reencola al reiniciar; sin tope
# entraría en un bucle infinito.
MAX_INTENTOS_POR_DEFECTO = 3

EjecutorLote = Callable[[Path, str], Awaitable[Any]]


class WorkerYaEnEjecucion(RuntimeError):
    """Otro worker tiene el candado de este `data_dir`."""


@dataclass(frozen=True)
class Reclamo:
    run_id: str
    ruta: Path  # en processing/


class WorkerLocal:
    def __init__(
        self,
        data_dir: Path,
        registro: RegistroIdempotencia,
        ejecutar: EjecutorLote,
        intervalo_s: float = 2.0,
        max_intentos: int = MAX_INTENTOS_POR_DEFECTO,
    ) -> None:
        self._data = Path(data_dir)
        self._raw = self._data / "raw"
        self._processing = self._data / "processing"
        self._processed = self._data / "processed"
        self._failed = self._data / "failed"
        self._registro = registro
        self._ejecutar = ejecutar
        self._intervalo = intervalo_s
        self._max_intentos = max_intentos
        self._candado: TextIO | None = None

    # -- exclusión mutua ----------------------------------------------------

    def adquirir_candado(self) -> None:
        self._data.mkdir(parents=True, exist_ok=True)
        f = open(self._data / "worker.lock", "w", encoding="utf-8")  # noqa: SIM115
        try:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover
                # Se bloquea SIN escribir antes (corrección probada en Windows por
                # Enrique): el byte 0 queda bloqueado por el primer worker y
                # escribirlo desde un segundo worker lanza PermissionError (también
                # al cerrar el archivo) en vez de WorkerYaEnEjecucion.
                f.seek(0)
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            f.close()
            raise WorkerYaEnEjecucion(f"ya hay un worker sobre {self._data}") from exc
        self._candado = f

    def liberar_candado(self) -> None:
        if self._candado is not None:
            try:
                if fcntl is None and msvcrt is not None:  # pragma: no cover
                    self._candado.seek(0)
                    msvcrt.locking(self._candado.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            finally:
                self._candado.close()  # cerrar libera el flock en POSIX
                self._candado = None

    # -- movimiento de archivos ---------------------------------------------

    _INTENTOS_ARCHIVADO = 3
    _ESPERA_ARCHIVADO_S = 0.2

    async def _archivar(self, origen: Path, destino: Path) -> bool:
        """Mueve un archivo ya procesado con reintentos breves.

        En Windows un antivirus o un sincronizador de nube puede tener el
        archivo abierto unos instantes (`PermissionError`). Como el estado
        terminal YA está registrado, un fallo persistente no debe tumbar al
        worker: el archivo queda en `processing/` y `preparar()` lo archiva en
        el siguiente arranque, sin reejecutar el lote.
        """
        for intento in range(1, self._INTENTOS_ARCHIVADO + 1):
            try:
                os.replace(origen, destino)
                return True
            except OSError as exc:
                log.warning("archivado_fallido", archivo=origen.name, intento=intento,
                            error=type(exc).__name__)
                if intento < self._INTENTOS_ARCHIVADO:
                    await asyncio.sleep(self._ESPERA_ARCHIVADO_S * intento)
        return False

    # -- preparación y recuperación ----------------------------------------

    def preparar(self) -> int:
        """Crea carpetas y resuelve lo que quedó en `processing/` tras una caída.

        * Run con estado terminal en el registro: el proceso cayó después de
          registrar el resultado y antes de archivar el archivo. Se termina el
          archivado SIN reejecutar.
        * Cualquier otro caso: se reencola en `raw/` (el ejecutor es idempotente
          por `run_id`).

        Devuelve cuántos lotes se reencolaron.
        """
        for d in (self._raw, self._processing, self._processed, self._failed):
            d.mkdir(parents=True, exist_ok=True)
        reencolados = 0
        for ruta in sorted(self._processing.iterdir()):
            m = _NOMBRE_RUN.match(ruta.name)
            if not m:
                continue
            run_id = m.group(1)
            fila = self._registro.obtener_por_run_id(run_id)
            if fila is not None and fila.estado in _TERMINALES:
                carpeta = self._failed if fila.estado == "fallido" else self._processed
                try:
                    os.replace(ruta, carpeta / ruta.name)
                except OSError as exc:
                    log.warning("archivado_tras_caida_fallido", run_id=run_id, error=type(exc).__name__)
                    continue
                log.warning("run_archivado_tras_caida", run_id=run_id, estado=fila.estado)
                continue
            try:
                os.replace(ruta, self._raw / ruta.name)
            except OSError as exc:
                log.warning("reencolado_fallido", run_id=run_id, error=type(exc).__name__)
                continue
            self._registro.actualizar_estado(run_id, "en_cola", solo_desde=("procesando",))
            log.warning("run_recuperado", run_id=run_id)
            reencolados += 1
        return reencolados

    # -- reclamo ------------------------------------------------------------

    def reclamar_siguiente(self) -> Reclamo | None:
        for ruta in sorted(self._raw.iterdir()):
            m = _NOMBRE_RUN.match(ruta.name)
            if not m:  # temp_*, basura, subcarpetas
                continue
            destino = self._processing / ruta.name
            try:
                # os.replace (no rename): mismo comportamiento atómico en POSIX y
                # Windows, donde rename falla si el destino ya existe.
                os.replace(ruta, destino)
            except FileNotFoundError:
                continue  # otro actor se lo llevó
            except OSError as exc:
                # Archivo bloqueado (antivirus, sincronizadores de nube en Windows)
                # u otro fallo de E/S: se salta y se reintenta en la próxima pasada
                # en vez de tumbar al worker.
                log.warning("reclamo_fallido", archivo=ruta.name, error=type(exc).__name__)
                continue
            return Reclamo(m.group(1), destino)
        return None

    # -- procesamiento ------------------------------------------------------

    async def procesar(self, reclamo: Reclamo) -> str:
        """Ejecuta un lote reclamado y devuelve el estado terminal registrado."""
        run_id = reclamo.run_id
        ligado = log.bind(run_id=run_id)
        # El intento se cuenta ANTES de ejecutar: si el proceso muere a mitad de
        # camino, el conteo ya quedó persistido.
        intentos = await asyncio.to_thread(self._registro.registrar_intento, run_id)
        if not await asyncio.to_thread(
            self._registro.actualizar_estado, run_id, "procesando", ("recibiendo", "en_cola")
        ):
            ligado.warning("run_sin_registro_o_estado_inesperado")

        estado = "fallido"
        if intentos is not None and intentos > self._max_intentos:
            ligado.error("run_excede_intentos", intentos=intentos, maximo=self._max_intentos)
        else:
            try:
                resultado = self._ejecutar(reclamo.ruta, run_id)
                if inspect.isawaitable(resultado):  # tolera ejecutores síncronos
                    resultado = await resultado
                candidato = getattr(resultado, "estado_flujo", None)
                if candidato in _TERMINALES:
                    estado = candidato
                    if estado == "fallido":
                        # El orquestador convierte sus excepciones técnicas en `fallido`
                        # (spec 5.2): sin este log, el motivo solo viviría en el resultado.
                        errores = [str(e)[:300] for e in (getattr(resultado, "errores", None) or [])[:3]]
                        ligado.error("run_fallido_por_el_pipeline", errores=errores)
                else:
                    ligado.error("estado_flujo_invalido", recibido=repr(candidato))
            except Exception:  # noqa: BLE001 - el worker nunca debe caerse por un lote
                ligado.exception("ejecutor_lanzo_excepcion")

        # Orden deliberado: primero el estado terminal en el registro y después
        # el archivado. Una caída entre ambos pasos deja el archivo en
        # `processing/` con estado terminal, y `preparar()` termina el archivado.
        # (Al revés, el run quedaba en `procesando` para siempre.)
        await asyncio.to_thread(self._registro.actualizar_estado, run_id, estado)
        carpeta = self._failed if estado == "fallido" else self._processed
        if not await self._archivar(reclamo.ruta, carpeta / reclamo.ruta.name):
            ligado.error("archivado_pendiente", estado=estado, archivo=reclamo.ruta.name)
        ligado.info("run_finalizado", estado=estado, intentos=intentos)
        return estado

    async def ejecutar_una_pasada(self) -> int:
        """Procesa todo lo que haya en raw/ ahora mismo. Devuelve cuántos."""
        n = 0
        while (reclamo := await asyncio.to_thread(self.reclamar_siguiente)) is not None:
            await self.procesar(reclamo)
            n += 1
        return n

    async def correr(self, parar: asyncio.Event | None = None) -> None:
        """Bucle principal. Se detiene cuando `parar` se activa."""
        parar = parar or asyncio.Event()
        self.adquirir_candado()
        try:
            self.preparar()
            log.info("worker_iniciado", data_dir=str(self._data))
            while not parar.is_set():
                if await self.ejecutar_una_pasada() == 0:
                    try:
                        await asyncio.wait_for(parar.wait(), timeout=self._intervalo)
                    except TimeoutError:
                        pass
        finally:
            self.liberar_candado()


# Métodos de `LocalDatabaseManager` que `CommunityLabPipeline` invoca (nodos 1 a 4 y
# manejo de fallos). Si faltan, CADA lote terminaría en `fallido` con un
# AttributeError y se iría archivando en `failed/`: es mejor no arrancar.
_METODOS_DB_REQUERIDOS = (
    "registrar_lote",
    "registrar_lote_y_analisis",
    "registrar_paquete",
    "leer_rechazados",
    "actualizar_estado_flujo",
)


def _cargar_ejecutor_por_defecto(db_path: Path | None = None) -> EjecutorLote:
    """Construye el orquestador y devuelve su `ejecutar_desde_archivo`.

    `db_path` es la base que usa el registro de la API; se pasa explícitamente a
    `LocalDatabaseManager` para que API, worker y orquestador escriban SIEMPRE en
    el mismo archivo, sin depender de qué variables de entorno vea cada uno.
    """
    try:
        from orchestration.pipeline_runner import CommunityLabPipeline
    except ModuleNotFoundError as exc:
        # Solo si falta el propio módulo. Un ImportError DENTRO de pipeline_runner
        # (p. ej. una dependencia sin instalar) debe verse tal cual, no disfrazado
        # de "aún no existe".
        if exc.name != "orchestration.pipeline_runner":
            raise
        raise SystemExit(
            "orchestration/pipeline_runner.py aún no existe: el worker necesita "
            "CommunityLabPipeline.ejecutar_desde_archivo (spec v3.1, sección 5.2)."
        ) from exc
    try:
        db_manager = None
        if db_path is not None:
            from storage.local_db import LocalDatabaseManager

            db_manager = LocalDatabaseManager(db_path)
        pipeline = CommunityLabPipeline(db_manager=db_manager)
    except Exception as exc:
        log.exception("pipeline_no_inicializable")
        raise SystemExit(
            f"No se pudo inicializar CommunityLabPipeline ({type(exc).__name__}): {exc}"
        ) from exc
    if hasattr(pipeline, "db_manager"):
        faltantes = [
            m for m in _METODOS_DB_REQUERIDOS if not callable(getattr(pipeline.db_manager, m, None))
        ]
        if faltantes:
            raise SystemExit(
                f"{type(pipeline.db_manager).__name__} no implementa lo que usa el orquestador: "
                f"{', '.join(faltantes)}. Sin eso todos los lotes terminarían en `fallido`; "
                "el worker no arranca."
            )
    return pipeline.ejecutar_desde_archivo


def main() -> None:
    from api.config import Settings
    from api.logging_config import configurar_logging

    try:
        cfg = Settings()
    except ValidationError as exc:
        # El worker comparte `.env` con la API (mismo `data_dir` y SQLite), por eso
        # hereda sus variables obligatorias; se falla rápido con un mensaje claro.
        campos = ", ".join(str(e["loc"][0]) for e in exc.errors())
        raise SystemExit(
            f"Configuración inválida para el worker (comparte .env con la API): {campos}. "
            "Define COMMUNITYLAB_WEBHOOK_SECRET (mín. 16 caracteres) y demás variables en .env."
        ) from exc
    configurar_logging(cfg.log_level)
    registro = RegistroIdempotencia(cfg.db_path, gracia_recepcion_s=cfg.gracia_recepcion_s)
    registro.inicializar()
    worker = WorkerLocal(cfg.data_dir, registro, _cargar_ejecutor_por_defecto(cfg.db_path))
    try:
        asyncio.run(worker.correr())
    except WorkerYaEnEjecucion as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()