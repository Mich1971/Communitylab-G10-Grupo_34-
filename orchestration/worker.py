"""Worker local: consume `data/raw/` y ejecuta el pipeline.

Cierra el hueco entre la API (que solo deposita lotes y responde 202) y el
orquestador (`CommunityLabPipeline.ejecutar_desde_archivo`).

Ciclo de vida de un archivo
---------------------------
    data/raw/{run_id}.json         depositado por la API (os.replace atómico)
      -> data/processing/{run_id}.json   reclamado con os.rename (atómico)
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

from api.registro import ESTADOS, RegistroIdempotencia

log = structlog.get_logger("orchestration.worker")

_NOMBRE_RUN = re.compile(r"^(run-\d{8}T\d{6}Z-[0-9a-f]{12})\.json$")
_TERMINALES = frozenset(
    {"completado", "sin_registros_validos", "sin_contenido_publicable", "fallido"}
)

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
    ) -> None:
        self._data = Path(data_dir)
        self._raw = self._data / "raw"
        self._processing = self._data / "processing"
        self._processed = self._data / "processed"
        self._failed = self._data / "failed"
        self._registro = registro
        self._ejecutar = ejecutar
        self._intervalo = intervalo_s
        self._candado: TextIO | None = None

    # -- exclusión mutua ----------------------------------------------------

    def adquirir_candado(self) -> None:
        self._data.mkdir(parents=True, exist_ok=True)
        f = open(self._data / "worker.lock", "w", encoding="utf-8")  # noqa: SIM115
        try:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:  # pragma: no cover
                # Se bloquea SIN escribir antes: en Windows el byte 0 queda
                # bloqueado por el primer worker y escribirlo desde un segundo
                # worker lanza PermissionError (también al cerrar el archivo)
                # en vez de WorkerYaEnEjecucion.
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

    # -- preparación y recuperación ----------------------------------------

    def preparar(self) -> int:
        """Crea carpetas y reencola lo que quedó a medias. Devuelve cuántos."""
        for d in (self._raw, self._processing, self._processed, self._failed):
            d.mkdir(parents=True, exist_ok=True)
        recuperados = 0
        for ruta in sorted(self._processing.iterdir()):
            m = _NOMBRE_RUN.match(ruta.name)
            if not m:
                continue
            os.replace(ruta, self._raw / ruta.name)
            self._registro.actualizar_estado(m.group(1), "en_cola", solo_desde=("procesando",))
            log.warning("run_recuperado", run_id=m.group(1))
            recuperados += 1
        return recuperados

    # -- reclamo ------------------------------------------------------------

    def reclamar_siguiente(self) -> Reclamo | None:
        for ruta in sorted(self._raw.iterdir()):
            m = _NOMBRE_RUN.match(ruta.name)
            if not m:  # temp_*, basura, subcarpetas
                continue
            destino = self._processing / ruta.name
            try:
                os.rename(ruta, destino)
            except FileNotFoundError:
                continue  # otro actor se lo llevó
            return Reclamo(m.group(1), destino)
        return None

    # -- procesamiento ------------------------------------------------------

    async def procesar(self, reclamo: Reclamo) -> str:
        """Ejecuta un lote reclamado y devuelve el estado terminal registrado."""
        run_id = reclamo.run_id
        ligado = log.bind(run_id=run_id)
        if not await asyncio.to_thread(
            self._registro.actualizar_estado, run_id, "procesando", ("recibiendo", "en_cola")
        ):
            ligado.warning("run_sin_registro_o_estado_inesperado")

        estado = "fallido"
        try:
            resultado = await self._ejecutar(reclamo.ruta, run_id)
            candidato = getattr(resultado, "estado_flujo", None)
            if candidato in _TERMINALES:
                estado = candidato
            else:
                ligado.error("estado_flujo_invalido", recibido=repr(candidato))
        except Exception:  # noqa: BLE001 - el worker nunca debe caerse por un lote
            ligado.exception("ejecutor_lanzo_excepcion")

        assert estado in ESTADOS
        carpeta = self._failed if estado == "fallido" else self._processed
        os.replace(reclamo.ruta, carpeta / reclamo.ruta.name)
        await asyncio.to_thread(self._registro.actualizar_estado, run_id, estado)
        ligado.info("run_finalizado", estado=estado)
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


def _cargar_ejecutor_por_defecto() -> EjecutorLote:
    try:
        from orchestration.pipeline_runner import CommunityLabPipeline
    except ImportError as exc:
        raise SystemExit(
            "orchestration/pipeline_runner.py aún no existe: el worker necesita "
            "CommunityLabPipeline.ejecutar_desde_archivo (spec v3.1, sección 5.2)."
        ) from exc
    return CommunityLabPipeline().ejecutar_desde_archivo


def main() -> None:
    from api.config import Settings
    from api.logging_config import configurar_logging

    cfg = Settings()
    configurar_logging(cfg.log_level)
    registro = RegistroIdempotencia(cfg.db_path, gracia_recepcion_s=cfg.gracia_recepcion_s)
    registro.inicializar()
    worker = WorkerLocal(cfg.data_dir, registro, _cargar_ejecutor_por_defecto())
    try:
        asyncio.run(worker.correr())
    except WorkerYaEnEjecucion as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()