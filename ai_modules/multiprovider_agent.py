"""F├íbrica multi-proveedor de agentes cognitivos (ai_modules/multiprovider_agent.py).

Construye el agente PydanticAI correspondiente al proveedor LLM activo
(OpenAI, Anthropic o Gemini) y configura, para cada uno, el mecanismo de
prompt caching que le corresponde seg├║n su propia arquitectura de API:

- OpenAI:    caching impl├¡cito por coincidencia exacta de prefijo. No requiere
             configuraci├│n expl├¡cita; basta con que el `system_prompt` (el
             cat├ílogo est├ítico de ejemplos) sea id├®ntico byte a byte entre
             invocaciones.
- Anthropic: caching declarativo mediante `AnthropicModelSettings
             (anthropic_cache_instructions=True)`, que inserta el punto de
             ruptura (`cache_control`) al final del bloque de instrucciones
             del sistema. Verificado contra la documentaci├│n oficial de
             PydanticAI (https://pydantic.dev/docs/ai/models/anthropic/).
- Gemini:    caching impl├¡cito de prefijo (modo stateless, sin crear un
             recurso `CachedContent`), para no introducir estado persistente
             (cache_id) en un agente que el resto del pipeline trata como
             una funci├│n pura sin memoria.

Punto cr├¡tico corregido respecto a la versi├│n anterior: el `model_settings`
calculado para cada proveedor se pasa al construir el `Agent`, en vez de
devolverse como un valor suelto que el llamador podr├¡a olvidar aplicar. As├¡
el agente retornado ya tiene su estrategia de cach├® activa para *todas* sus
ejecuciones, sin depender de que cada `.run()` la repita.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel

logger = structlog.get_logger("ai_modules.cache_orchestrator")

PROVEEDORES_SOPORTADOS = ("openai", "anthropic", "gemini")

# TTL por defecto para la cach├® de instrucciones del sistema en Anthropic.
# '5m' (valor por defecto del propio proveedor) es suficiente para lotes de
# procesamiento consecutivos; se deja como constante para no enterrar un
# "magic value" dentro de la l├│gica de construcci├│n del agente.
ANTHROPIC_CACHE_TTL = "5m"

# Modelos por defecto y variable de entorno que los sustituye. Los
# identificadores se retiran con el tiempo (claude-3-5-haiku-20241022 y
# gemini-1.5-flash ya no existen), as├¡ que NINGUNO queda fijo en el c├│digo:
# cambiar de modelo es cambiar una variable de entorno, no editar y desplegar.
MODELOS_POR_DEFECTO = {
    "openai": ("OPENAI_MODEL", "gpt-4o-mini"),
    "anthropic": ("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
    "gemini": ("GEMINI_MODEL", "gemini-2.5-flash"),
}


def resolver_modelo(provider: str) -> str:
    """Nombre del modelo del proveedor: variable de entorno o valor por defecto.

    Una variable vac├¡a o solo con espacios se ignora (un `.env` con
    `ANTHROPIC_MODEL=` no debe producir un modelo con nombre vac├¡o).
    """
    variable, defecto = MODELOS_POR_DEFECTO[provider.lower()]
    return (os.environ.get(variable) or "").strip() or defecto


class ProveedorNoSoportadoError(ValueError):
    """El valor de LLM_PROVIDER no corresponde a ning├║n proveedor implementado."""


class CognitiveAgentFactory:
    """F├íbrica multi-proveedor que ensambla el agente y activa, de una vez,
    la estrategia de cach├® correspondiente, sin romper el hot-swapping.
    """

    @staticmethod
    def create_agent(provider: str, static_catalog_prompt: str, result_schema: Any) -> Agent:
        """Construye el `Agent` de PydanticAI ya configurado para el proveedor dado.

        Args:
            provider: "openai" | "anthropic" | "gemini" (no sensible a may├║sculas).
            static_catalog_prompt: el prefijo est├ítico e inmutable del system
                prompt (instrucciones + cat├ílogo de ejemplos few-shot). Debe
                ser id├®ntico byte a byte entre invocaciones para que la cach├®
                de cualquiera de los tres proveedores pueda acertar.
            result_schema: el modelo Pydantic de salida esperado (p. ej.
                `MensajeAnalizado`).

        Returns:
            Un `Agent` listo para `.run()`/`.run_sync()`, con el
            `model_settings` de cach├® ya aplicado a nivel de constructor
            (de modo que se use autom├íticamente en cada ejecuci├│n).

        Raises:
            ProveedorNoSoportadoError: si `provider` no es uno de los
                valores soportados.
        """
        provider_normalizado = provider.lower()

        if provider_normalizado == "openai":
            # Caching impl├¡cito: OpenAI detecta por s├¡ mismo la coincidencia
            # de prefijo cuando el system_prompt es id├®ntico entre llamadas.
            # No existe un model_settings de cach├® que activar expl├¡citamente.
            model = OpenAIChatModel(resolver_modelo("openai"))
            agent = Agent(
                model=model,
                output_type=result_schema,
                system_prompt=static_catalog_prompt,
            )

        elif provider_normalizado == "anthropic":
            # Caching declarativo: se activa mediante AnthropicModelSettings,
            # pasado como model_settings del Agent para que se aplique a
            # todas sus ejecuciones sin que el llamador deba repetirlo.
            model = AnthropicModel(resolver_modelo("anthropic"))
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
            # Caching impl├¡cito de prefijo (API v1beta, modo stateless):
            # se evita deliberadamente el recurso CachedContent expl├¡cito
            # para no introducir un cache_id que el backend tendr├¡a que
            # persistir, rompiendo el dise├▒o sin estado del agente.
            model = GoogleModel(resolver_modelo("gemini"))
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
    def read_usage(resultado: Any) -> Any:
        """Uso de tokens de un resultado de `agent.run()`, sea `usage` m├®todo o propiedad.

        En PydanticAI 2.x `AgentRunResult.usage` es una PROPIEDAD; en 1.x era el
        m├®todo `usage()`. Llamar siempre `usage()` lanzaba `TypeError` con la
        versi├│n actual y hac├¡a degradar CADA mensaje como si el LLM estuviera
        ca├¡do. Esta funci├│n acepta ambas formas.
        """
        uso = resultado.usage
        return uso() if callable(uso) else uso

    @staticmethod
    def extract_cached_tokens(provider: str, raw_usage: Any) -> int:
        """Normaliza la telemetr├¡a de tokens cacheados para structlog y
        `MetadatosEjecucion`, dado que cada proveedor expone el dato bajo un
        campo distinto en su respuesta de uso:

        - OpenAI:    usage.prompt_tokens_details.cached_tokens
        - Anthropic: usage.cache_read_input_tokens
        - Gemini:    usage.cached_content_token_count

        Cualquier fallo de atributo o proveedor desconocido degrada a 0 en
        vez de propagar la excepci├│n, ya que esta funci├│n solo alimenta
        observabilidad y nunca debe interrumpir el procesamiento del lote.
        """
        provider_normalizado = provider.lower()
        try:
            # CORRECCI├ôN: PydanticAI ya normaliza el dato entre proveedores en
            # `RunUsage.cache_read_tokens`. Los atributos espec├¡ficos de cada
            # proveedor (abajo) no existen en ese objeto, as├¡ que antes esta
            # funci├│n devolv├¡a siempre 0 con el `usage()` real de un agente.
            normalizado = getattr(raw_usage, "cache_read_tokens", None)
            if isinstance(normalizado, int) and not isinstance(normalizado, bool) and normalizado > 0:
                return normalizado
            if provider_normalizado == "openai":
                details = getattr(raw_usage, "prompt_tokens_details", None)
                return getattr(details, "cached_tokens", 0) if details is not None else 0
            if provider_normalizado == "anthropic":
                return getattr(raw_usage, "cache_read_input_tokens", 0) or 0
            if provider_normalizado == "gemini":
                return getattr(raw_usage, "cached_content_token_count", 0) or 0
        except Exception:  # noqa: BLE001 ÔÇö telemetr├¡a best-effort, nunca debe tumbar el lote
            logger.warning("fallo_extraccion_tokens_cacheados", proveedor=provider_normalizado)
            return 0
        return 0

    @staticmethod
    def extract_token_counts(raw_usage: Any) -> tuple[int, int]:
        """Devuelve `(tokens_entrada, tokens_salida)` de una respuesta.

        Usa los campos normalizados de `RunUsage` (`input_tokens`,
        `output_tokens`), id├®nticos para los tres proveedores. Como el resto
        de la telemetr├¡a, es de mejor esfuerzo: ante cualquier fallo devuelve
        `(0, 0)` en vez de interrumpir el procesamiento del lote.
        """
        try:
            entrada = getattr(raw_usage, "input_tokens", 0)
            salida = getattr(raw_usage, "output_tokens", 0)
            entrada = entrada if isinstance(entrada, int) and not isinstance(entrada, bool) else 0
            salida = salida if isinstance(salida, int) and not isinstance(salida, bool) else 0
            return max(0, entrada), max(0, salida)
        except Exception:  # noqa: BLE001 ÔÇö telemetr├¡a best-effort, nunca debe tumbar el lote
            logger.warning("fallo_extraccion_tokens")
            return 0, 0
