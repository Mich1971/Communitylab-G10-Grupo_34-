"""Paquete de prompts del analizador cognitivo.

Re-exporta `SYSTEM_PROMPT` para que `ai_modules.cognitive_analyzer` pueda
importarlo directamente desde el paquete (`from ai_modules.prompts import
SYSTEM_PROMPT`), sin acoplarse al submódulo interno donde se ensambla.
"""

from .analyzer import REANCLAJE_INSTRUCCIONES, SYSTEM_PROMPT
from .catalog import CATALOGO_FEW_SHOT
from .instructions import INSTRUCCIONES_BASE

__all__ = [
    "SYSTEM_PROMPT",
    "REANCLAJE_INSTRUCCIONES",
    "CATALOGO_FEW_SHOT",
    "INSTRUCCIONES_BASE",
]
