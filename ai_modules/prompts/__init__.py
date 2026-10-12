"""Prompts estaticos para el analisis cognitivo de interacciones."""

from .analyzer import SYSTEM_PROMPT
from .catalog import CATALOGO_FEW_SHOT
from .instructions import INSTRUCCIONES_BASE

__all__ = ["CATALOGO_FEW_SHOT", "INSTRUCCIONES_BASE", "SYSTEM_PROMPT"]