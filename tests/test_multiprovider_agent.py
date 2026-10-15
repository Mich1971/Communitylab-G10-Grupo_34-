"""Pruebas unitarias para ai_modules/multiprovider_agent.py.

Cubren el defecto detectado en la versión previa: que el `model_settings`
calculado para Anthropic se aplicara realmente al `Agent` construido, en
vez de quedar como un valor suelto sin conectar.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ai_modules.multiprovider_agent import (
    CognitiveAgentFactory,
    ProveedorNoSoportadoError,
)


class _ResultSchemaFicticio(BaseModel):
    """Placeholder de esquema Pydantic de salida; no se instancia en estas pruebas."""


# --- construcción del agente por proveedor --------------------------------


def test_openai_no_requiere_model_settings_explicito():
    agent = CognitiveAgentFactory.create_agent("openai", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == "gpt-4o-mini"


def test_gemini_no_requiere_model_settings_explicito():
    agent = CognitiveAgentFactory.create_agent("gemini", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == "gemini-1.5-flash"


def test_anthropic_activa_cache_de_instrucciones_del_sistema():
    """Regresión del defecto principal: el agente de Anthropic debe llevar
    `anthropic_cache_instructions` en su model_settings, no solo calculado
    y descartado."""
    agent = CognitiveAgentFactory.create_agent("anthropic", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == "claude-3-5-haiku-20241022"
    assert agent.model_settings is not None
    assert agent.model_settings.get("anthropic_cache_instructions") == "5m"


@pytest.mark.parametrize("provider", ["OpenAI", "ANTHROPIC", "Gemini"])
def test_create_agent_no_distingue_mayusculas(provider):
    # No debe lanzar excepción con variantes de capitalización del proveedor.
    CognitiveAgentFactory.create_agent(provider, "catalogo", _ResultSchemaFicticio)


def test_proveedor_no_soportado_lanza_error_especifico():
    with pytest.raises(ProveedorNoSoportadoError):
        CognitiveAgentFactory.create_agent("mistral", "catalogo", _ResultSchemaFicticio)


# --- normalización de telemetría de tokens cacheados -----------------------


class _UsageOpenAI:
    class _Details:
        cached_tokens = 128

    prompt_tokens_details = _Details()


class _UsageAnthropic:
    cache_read_input_tokens = 256


class _UsageGemini:
    cached_content_token_count = 512


def test_extract_cached_tokens_openai():
    assert CognitiveAgentFactory.extract_cached_tokens("openai", _UsageOpenAI()) == 128


def test_extract_cached_tokens_anthropic():
    assert CognitiveAgentFactory.extract_cached_tokens("anthropic", _UsageAnthropic()) == 256


def test_extract_cached_tokens_gemini():
    assert CognitiveAgentFactory.extract_cached_tokens("gemini", _UsageGemini()) == 512


def test_extract_cached_tokens_degrada_a_cero_sin_lanzar_excepcion():
    objeto_sin_atributos = object()
    assert CognitiveAgentFactory.extract_cached_tokens("openai", objeto_sin_atributos) == 0
    assert CognitiveAgentFactory.extract_cached_tokens("proveedor-desconocido", objeto_sin_atributos) == 0
