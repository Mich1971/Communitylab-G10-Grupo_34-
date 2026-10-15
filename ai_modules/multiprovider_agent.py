"""Fábrica multi-proveedor de agentes cognitivos (ai_modules/multiprovider_agent.py).

Construye el agente PydanticAI correspondiente al proveedor LLM activo
(OpenAI, Anthropic o Gemini) y configura, para cada uno, el mecanismo de
prompt caching que le corresponde según su propia arquitectura de API:

- OpenAI:    caching implícito por coincidencia exacta de prefijo. No requiere
             configuración explícita; basta con que el `system_prompt` (el
             catálogo estático de ejemplos) sea idéntico byte a byte entre
             invocaciones.
- Anthropic: caching declarativo mediante `AnthropicModelSettings
             (anthropic_cache_instructions=True)`, que inserta el punto de
             ruptura (`cache_control`) al final del bloque de instrucciones
             del sistema. Verificado contra la documentación oficial de
             PydanticAI (https://pydantic.dev/docs/ai/models/anthropic/).
- Gemini:    caching implícito de prefijo (modo stateless, sin crear un
             recurso `CachedContent`), para no introducir estado persistente
             (cache_id) en un agente que el resto del pipeline trata como
             una función pura sin memoria.

Punto crítico corregido respecto a la versión anterior: el `model_settings`
calculado para cada proveedor se pasa al construir el `Agent`, en vez de
devolverse como un valor suelto que el llamador podría olvidar aplicar. Así
el agente retornado ya tiene su estrategia de caché activa para *todas* sus
ejecuciones, sin depender de que cada `.run()` la repita.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

logger = structlog.get_logger("ai_modules.cache_orchestrator")

PROVEEDORES_SOPORTADOS = ("openai", "anthropic", "gemini")

# TTL por defecto para la caché de instrucciones del sistema en Anthropic.
# '5m' (valor por defecto del propio proveedor) es suficiente para lotes de
# procesamiento consecutivos; se deja como constante para no enterrar un
# "magic value" dentro de la lógica de construcción del agente.
ANTHROPIC_CACHE_TTL = "5m"


class ProveedorNoSoportadoError(ValueError):
    """El valor de LLM_PROVIDER no corresponde a ningún proveedor implementado."""


class CognitiveAgentFactory:
    """Fábrica multi-proveedor que ensambla el agente y activa, de una vez,
    la estrategia de caché correspondiente, sin romper el hot-swapping.
    """

    @staticmethod
    def create_agent(provider: str, static_catalog_prompt: str, result_schema: Any) -> Agent:
        """Construye el `Agent` de PydanticAI ya configurado para el proveedor dado.

        Args:
            provider: "openai" | "anthropic" | "gemini" (no sensible a mayúsculas).
            static_catalog_prompt: el prefijo estático e inmutable del system
                prompt (instrucciones + catálogo de ejemplos few-shot). Debe
                ser idéntico byte a byte entre invocaciones para que la caché
                de cualquiera de los tres proveedores pueda acertar.
            result_schema: el modelo Pydantic de salida esperado (p. ej.
                `MensajeAnalizado`).

        Returns:
            Un `Agent` listo para `.run()`/`.run_sync()`, con el
            `model_settings` de caché ya aplicado a nivel de constructor
            (de modo que se use automáticamente en cada ejecución).

        Raises:
            ProveedorNoSoportadoError: si `provider` no es uno de los
                valores soportados.
        """
        provider_normalizado = provider.lower()

        if provider_normalizado == "openai":
            # Caching implícito: OpenAI detecta por sí mismo la coincidencia
            # de prefijo cuando el system_prompt es idéntico entre llamadas.
            # No existe un model_settings de caché que activar explícitamente.
            model = OpenAIChatModel("gpt-4o-mini")
            agent = Agent(
                model=model,
                output_type=result_schema,
                system_prompt=static_catalog_prompt,
            )

        elif provider_normalizado == "anthropic":
            # Caching declarativo: se activa mediante AnthropicModelSettings,
            # pasado como model_settings del Agent para que se aplique a
            # todas sus ejecuciones sin que el llamador deba repetirlo.
            model = AnthropicModel("claude-3-5-haiku-20241022")
            settings: AnthropicModelSettings = {
                "anthropic_cache_instructions": ANTHROPIC_CACHE_TTL,
            }
            agent = Agent(
                model=model,
                output_type=result_schema,
                system_prompt=static_catalog_prompt,
                model_settings=settings,
            )

        elif provider_normalizado == "gemini":
            # Caching implícito de prefijo (API v1beta, modo stateless):
            # se evita deliberadamente el recurso CachedContent explícito
            # para no introducir un cache_id que el backend tendría que
            # persistir, rompiendo el diseño sin estado del agente.
            model = GoogleModel("gemini-1.5-flash")
            agent = Agent(
                model=model,
                output_type=result_schema,
                system_prompt=static_catalog_prompt,
            )

        else:
            raise ProveedorNoSoportadoError(f"Proveedor LLM no soportado: {provider!r}")

        logger.info(
            "agente_cognitivo_construido",
            proveedor=provider_normalizado,
            modelo=model.model_name,
        )
        return agent

    @staticmethod
    def extract_cached_tokens(provider: str, raw_usage: Any) -> int:
        """Normaliza la telemetría de tokens cacheados para structlog y
        `MetadatosEjecucion`, dado que cada proveedor expone el dato bajo un
        campo distinto en su respuesta de uso:

        - OpenAI:    usage.prompt_tokens_details.cached_tokens
        - Anthropic: usage.cache_read_input_tokens
        - Gemini:    usage.cached_content_token_count

        Cualquier fallo de atributo o proveedor desconocido degrada a 0 en
        vez de propagar la excepción, ya que esta función solo alimenta
        observabilidad y nunca debe interrumpir el procesamiento del lote.
        """
        provider_normalizado = provider.lower()
        try:
            if provider_normalizado == "openai":
                details = getattr(raw_usage, "prompt_tokens_details", None)
                return getattr(details, "cached_tokens", 0) if details is not None else 0
            if provider_normalizado == "anthropic":
                return getattr(raw_usage, "cache_read_input_tokens", 0) or 0
            if provider_normalizado == "gemini":
                return getattr(raw_usage, "cached_content_token_count", 0) or 0
        except Exception:  # noqa: BLE001 — telemetría best-effort, nunca debe tumbar el lote
            logger.warning("fallo_extraccion_tokens_cacheados", proveedor=provider_normalizado)
            return 0
        return 0
