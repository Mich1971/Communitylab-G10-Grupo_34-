"""structlog en JSON con redacción de PII (decisión de arquitectura).

La API nunca registra el cuerpo de la petición ni el secreto; esta capa es
la segunda barrera: si algo con forma de correo, teléfono o token llega a un
evento (p. ej. un `request_id` raro enviado por el cliente), se enmascara.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

# CORRECCIÓN (Riesgo 5): Se eliminó el carácter '.' de la clase de separadores
# y se añadieron lookarounds negativos para puntos (?<![\w/.]) y (?![\w/.]),
# evitando que direcciones IPv4 (ej. 10.0.10.245) o versiones semánticas sean
# enmascaradas por error como "[telefono]" en los logs de la API, worker y runner.
_PATRONES = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "bearer [redactado]"),
    (
        re.compile(r"(?i)\b(token|secret|api[_-]?key|authorization)\b\s*[:=]?\s*\S+"),
        r"\1=[redactado]",
    ),
    # El `(?!\d{4}-\d{2}-\d{2}\b)` evita enmascarar fechas ISO simples (2026-10-18).
    (re.compile(r"(?<![\w/.])(?!\d{4}-\d{2}-\d{2}\b)\+?\d[\d\s()-]{7,}\d(?![\w/.])"), "[telefono]"),
)


def redactar(valor: str) -> str:
    """Aplica las reglas de redacción de PII sobre una cadena de texto."""
    for patron, reemplazo in _PATRONES:
        valor = patron.sub(reemplazo, valor)
    return valor


def _redactar_pii(_logger: Any, _metodo: str, evento: dict[str, Any]) -> dict[str, Any]:
    for clave, valor in evento.items():
        if isinstance(valor, str):
            evento[clave] = redactar(valor)
    return evento


class _StdoutDiferido:
    """Escribe en el `sys.stdout` vigente en cada llamada.

    structlog es global: si se fijara el objeto `sys.stdout` al configurar,
    cualquier redirección posterior (pytest, uvicorn, un servicio que cierra
    su stream) dejaría a TODOS los módulos con un archivo cerrado.
    """

    def write(self, texto: str) -> int:
        return sys.stdout.write(texto)

    def flush(self) -> None:
        sys.stdout.flush()


def configurar_logging(nivel: str = "INFO") -> None:
    """Configura structlog para emitir eventos JSON con redacción de PII."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _redactar_pii,  # antes del sello de tiempo: el ISO-8601 parece un teléfono
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(nivel)),
        logger_factory=structlog.PrintLoggerFactory(file=_StdoutDiferido()),
        cache_logger_on_first_use=False,
    )
