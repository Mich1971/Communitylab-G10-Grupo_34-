"""Pruebas unitarias para ai_modules/multiprovider_agent.py.

Cubren el defecto detectado en la versión previa: que el `model_settings`
calculado para Anthropic se aplicara realmente al `Agent` construido, en
vez de quedar como un valor suelto sin conectar.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

import ai_modules.multiprovider_agent as M
from ai_modules.multiprovider_agent import (
    CognitiveAgentFactory,
    ProveedorNoSoportadoError,
)


@pytest.fixture(autouse=True)
def _entorno_hermetico(monkeypatch):
    """Claves ficticias (construir el agente las exige) y sin *_MODEL heredados."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(var, "clave-de-prueba")
    for var, _ in M.MODELOS_POR_DEFECTO.values():
        monkeypatch.delenv(var, raising=False)


class _ResultSchemaFicticio(BaseModel):
    """Placeholder de esquema Pydantic de salida; no se instancia en estas pruebas."""


# --- construcción del agente por proveedor --------------------------------


def test_openai_no_requiere_model_settings_explicito():
    agent = CognitiveAgentFactory.create_agent("openai", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == M.MODELOS_POR_DEFECTO["openai"][1]


def test_gemini_no_requiere_model_settings_explicito():
    agent = CognitiveAgentFactory.create_agent("gemini", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == M.MODELOS_POR_DEFECTO["gemini"][1]


def test_anthropic_activa_cache_de_instrucciones_del_sistema():
    """Regresión del defecto principal: el agente de Anthropic debe llevar
    `anthropic_cache_instructions` en su model_settings, no solo calculado
    y descartado."""
    agent = CognitiveAgentFactory.create_agent("anthropic", "catalogo estatico", _ResultSchemaFicticio)
    assert agent.model.model_name == M.MODELOS_POR_DEFECTO["anthropic"][1]
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


# --- compatibilidad con el `usage()` real de PydanticAI (RunUsage) ----------


def test_extract_cached_tokens_lee_el_campo_normalizado_de_run_usage():
    """Regresión: antes solo se leían atributos propios de cada proveedor, que
    no existen en `RunUsage`, así que el valor real siempre salía 0."""
    from pydantic_ai.usage import RunUsage

    uso = RunUsage(input_tokens=1000, output_tokens=50, cache_read_tokens=700)
    for proveedor in ("openai", "anthropic", "gemini"):
        assert CognitiveAgentFactory.extract_cached_tokens(proveedor, uso) == 700


def test_extract_token_counts_lee_entrada_y_salida_de_run_usage():
    from pydantic_ai.usage import RunUsage

    uso = RunUsage(input_tokens=1000, output_tokens=50)
    assert CognitiveAgentFactory.extract_token_counts(uso) == (1000, 50)


def test_extract_token_counts_degrada_a_cero_sin_lanzar_excepcion():
    assert CognitiveAgentFactory.extract_token_counts(object()) == (0, 0)
    assert CognitiveAgentFactory.extract_token_counts(None) == (0, 0)


# --- modelo configurable por entorno -----------------------------------------


def test_el_modelo_de_anthropic_ya_no_es_uno_retirado():
    assert M.MODELOS_POR_DEFECTO["anthropic"][1] != "claude-3-5-haiku-20241022"
    assert M.MODELOS_POR_DEFECTO["gemini"][1] != "gemini-1.5-flash"


@pytest.mark.parametrize("proveedor", ["openai", "anthropic", "gemini"])
def test_el_modelo_se_lee_de_la_variable_de_entorno(proveedor, monkeypatch):
    variable, _ = M.MODELOS_POR_DEFECTO[proveedor]
    monkeypatch.setenv(variable, "modelo-x-1")
    agent = CognitiveAgentFactory.create_agent(proveedor, "catalogo", _ResultSchemaFicticio)
    assert agent.model.model_name == "modelo-x-1"


def test_variable_de_modelo_vacia_o_con_espacios_usa_el_valor_por_defecto(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "   ")
    assert M.resolver_modelo("anthropic") == M.MODELOS_POR_DEFECTO["anthropic"][1]
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    assert M.resolver_modelo("Anthropic") == M.MODELOS_POR_DEFECTO["anthropic"][1]


# --- read_usage: propiedad (pydantic-ai 2.x) o método (1.x) --------------------


def test_read_usage_acepta_propiedad_y_metodo():
    uso = object()

    class _Propiedad:
        usage = uso

    class _Metodo:
        def usage(self):
            return uso

    assert CognitiveAgentFactory.read_usage(_Propiedad()) is uso
    assert CognitiveAgentFactory.read_usage(_Metodo()) is uso


def test_read_usage_funciona_con_el_resultado_real_del_sdk():
    """Regresión: con el SDK real, `resultado.usage()` lanzaba TypeError."""
    agente = Agent(TestModel(custom_output_text="hola"))
    resultado = asyncio.run(agente.run("hola"))
    uso = CognitiveAgentFactory.read_usage(resultado)
    entrada, salida = CognitiveAgentFactory.extract_token_counts(uso)
    assert entrada > 0 and salida > 0
