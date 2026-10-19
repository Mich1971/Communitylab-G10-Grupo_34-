"""Pruebas unitarias aisladas (sin API externa) de ai_modules/cognitive_analyzer.py."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from ai_modules import cognitive_analyzer as ca
from ai_modules.prompts import PROMPT_VERSION, version_de_prompt
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadatosEjecucion,
    PuntuacionRelevancia,
)


def _interaccion(i: int, texto: str = "Terminé el módulo de LangGraph hoy") -> InteraccionValidada:
    """Construye una instancia válida de InteraccionValidada para pruebas."""
    return InteraccionValidada(
        id=f"msg_{i:03d}",
        autor="a",
        canal="logros",
        tipo="mensaje",
        texto=texto,
        metadata_origen={
            "plataforma": "Discord",
            "identificador_original": f"d{i}",
            "fecha": datetime(2026, 9, 20, tzinfo=UTC),
        },
    )


def _mensaje(source_id: str, sent: str = "Positivo", soporte: bool = False) -> MensajeAnalizado:
    """Construye un MensajeAnalizado simulado de prueba."""
    return MensajeAnalizado(
        source_id=source_id,
        sentimiento=sent,  # type: ignore[arg-type]
        categoria_enrutamiento="Logro",
        regla_aplicada="regla de prueba",
        temas=["LangGraph"],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=2, utilidad_comunitaria=1, claridad_contexto=1
        ),
        motivo_seleccion="Justificación suficiente para la prueba.",
        requiere_soporte=soporte,
        apto_para_publicacion=True,
    )


class _Resultado:
    def __init__(self, output: Any):
        self.output = output

    def usage(self) -> object:
        return object()


class _AgenteFalso:
    """Falla `fallos` veces con `error` y luego responde con source_id ajeno (a propósito)."""

    def __init__(
        self,
        fallos: int = 0,
        error: Exception | None = None,
        model_settings: dict[str, Any] | None = None,
    ):
        self.llamadas = 0
        self.fallos = fallos
        self.error = error or RuntimeError("503")
        self.model_settings = model_settings
        self.settings: list[dict[str, Any] | None] = []

    async def run(self, entrada: str, model_settings: dict[str, Any] | None = None) -> _Resultado:
        self.llamadas += 1
        self.settings.append(model_settings)
        if self.llamadas <= self.fallos:
            raise self.error
        return _Resultado(_mensaje("id_inventado_por_el_llm"))


class _AgenteSegunContenido:
    """Decide su respuesta según qué `source_id` aparece en la entrada
    serializada, no según el orden de llamada — las interacciones se
    procesan de forma concurrente vía `asyncio.gather`, así que depender
    del orden de llegada sería una prueba frágil.
    """

    def __init__(self, ids_que_fallan: set[str], error: Exception | None = None):
        self.ids_que_fallan = ids_que_fallan
        self.error = error or RuntimeError("503")
        self.llamadas = 0

    async def run(self, entrada: str, model_settings: dict[str, Any] | None = None) -> _Resultado:
        self.llamadas += 1
        if any(
            f'"id":"{id_}"' in entrada or f'"id": "{id_}"' in entrada
            for id_ in self.ids_que_fallan
        ):
            raise self.error
        return _Resultado(_mensaje("id_inventado_por_el_llm"))


@pytest.fixture(autouse=True)
def _sin_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _nada(_: float) -> None:
        return None

    monkeypatch.setattr(ca.asyncio, "sleep", _nada)


def _usar_agente(monkeypatch: pytest.MonkeyPatch, agente: Any) -> None:
    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", lambda *a, **k: agente)


def test_enmascara_pii_y_preserva_datos_tecnicos() -> None:
    t = ca._enmascarar_pii(
        "Escríbeme a ana@mail.com o al +52 55 1234 5678, api_key=abc123. "
        "Falla en IP 10.0.10.245 con versión 1.12.3-20261013."
    )
    assert "ana@mail.com" not in t and "1234 5678" not in t and "abc123" not in t
    assert (
        "[EMAIL_PROTEGIDO]" in t
        and "[TELEFONO_PROTEGIDO]" in t
        and "[CREDENCIAL_PROTEGIDA]" in t
    )
    # Mitigación Riesgo 5: No debe enmascarar direcciones IPv4 ni versiones con puntos
    assert "10.0.10.245" in t
    assert "1.12.3-20261013" in t


def test_truncamiento_conserva_proporcion_20_80() -> None:
    corto = "x" * 100
    assert ca._truncar_texto_semantico(corto) == corto
    largo = "A" * 3000 + "Z" * 3000
    r = ca._truncar_texto_semantico(largo, max_chars=1000)
    assert r.startswith("A" * 200) and r.endswith("Z" * 800)
    assert "truncado" in r


def test_resumen_determinista() -> None:
    msgs = [
        _mensaje("a", "Positivo"),
        _mensaje("b", "Positivo", soporte=True),
        _mensaje("c", "Neutro"),
    ]
    r = ca._calcular_resumen_comunidad(msgs, 4)
    assert r.sentimiento_predominante == "Positivo"
    assert r.distribucion_sentimiento == {
        "Altamente Positivo": 0,
        "Positivo": 2,
        "Neutro": 1,
        "Dificultad/Negativo": 0,
    }
    assert r.total_interacciones_procesadas == 4 and r.registros_validos == 3
    assert len(r.alertas_soporte) == 1 and r.alertas_soporte[0].startswith("[b]")


def test_resumen_detecta_empate_y_reporta_mixto() -> None:
    """CORRECCIÓN: `max(conteo, key=...)` resolvía cualquier empate a una
    sola categoría arbitraria. El esquema documenta "Mixto" explícitamente
    para este caso (`ResumenComunidad.sentimiento_predominante`).
    """
    msgs = [
        _mensaje("a", "Positivo"),
        _mensaje("b", "Dificultad/Negativo"),
    ]
    r = ca._calcular_resumen_comunidad(msgs, 2)
    assert r.sentimiento_predominante == "Mixto"


def test_resumen_sin_empate_no_reporta_mixto() -> None:
    msgs = [
        _mensaje("a", "Positivo"),
        _mensaje("b", "Positivo"),
        _mensaje("c", "Neutro"),
    ]
    r = ca._calcular_resumen_comunidad(msgs, 3)
    assert r.sentimiento_predominante == "Positivo"


def test_degradado_preserva_source_id_y_score_cero() -> None:
    m = ca._crear_mensaje_degradado("msg_009", RuntimeError("boom"))
    assert m.source_id == "msg_009"
    assert m.puntuacion_relevancia.total == 0 and not m.apto_para_publicacion
    assert "RuntimeError" in m.motivo_seleccion and "boom" not in m.motivo_seleccion
    assert m.requiere_soporte is True
    assert m.categoria_enrutamiento == "Dificultad"
    assert ca.es_mensaje_degradado(m) is True


def test_flujo_nominal_fuerza_source_id_original(monkeypatch: pytest.MonkeyPatch) -> None:
    _usar_agente(monkeypatch, _AgenteFalso())
    lote = [_interaccion(1), _interaccion(2)]
    r = asyncio.run(ca.analizar_lote_cognitivo(lote, "run-1", "req-1", provider="openai"))
    assert [m.source_id for m in r.mensajes_analizados] == ["msg_001", "msg_002"]
    assert r.run_id == "run-1" and r.request_id == "req-1"
    assert ca.tiene_analisis_utilizable(r) is True


def test_reintento_transitorio_se_recupera(monkeypatch: pytest.MonkeyPatch) -> None:
    agente = _AgenteFalso(fallos=2)
    _usar_agente(monkeypatch, agente)
    r = asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="gemini"))
    assert agente.llamadas == 3
    assert r.resumen_comunidad.registros_rechazados == 0


def test_agotamiento_degrada_sin_romper_lote_y_mantiene_coherencia_matematica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agente = _AgenteFalso(fallos=999)
    _usar_agente(monkeypatch, agente)
    lote = [_interaccion(1), _interaccion(2)]
    r, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria(lote, "r", "q", provider="anthropic")
    )
    assert agente.llamadas == 2 * (ca.MAX_REINTENTOS_DEFECTO + 1)
    assert [m.source_id for m in r.mensajes_analizados] == ["msg_001", "msg_002"]
    assert all(m.puntuacion_relevancia.total == 0 for m in r.mensajes_analizados)
    # Mitigación Riesgo 3: validos (1) + rechazados (1) == total (2) en el contrato,
    # mientras la telemetría reporta los 2 degradados reales y caida_total_llm = True.
    assert r.resumen_comunidad.registros_validos == 1
    assert r.resumen_comunidad.registros_rechazados == 1
    assert (
        r.resumen_comunidad.registros_validos + r.resumen_comunidad.registros_rechazados
        == r.resumen_comunidad.total_interacciones_procesadas
    )
    assert tel.degradados == 2
    assert tel.caida_total_llm is True
    assert ca.tiene_analisis_utilizable(r) is False
    assert any("degradados" in a for a in r.resumen_comunidad.alertas_soporte)


def test_degradacion_parcial_queda_visible_en_alertas_soporte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caso realista: solo uno de dos mensajes degrada."""
    agente = _AgenteSegunContenido(ids_que_fallan={"msg_001"})
    _usar_agente(monkeypatch, agente)
    lote = [_interaccion(1), _interaccion(2)]
    r, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria(lote, "r", "q", provider="openai")
    )

    assert r.resumen_comunidad.registros_validos == 1
    assert r.resumen_comunidad.registros_rechazados == 1
    assert tel.caida_total_llm is False
    assert ca.tiene_analisis_utilizable(r) is True
    assert any(a.startswith("[msg_001]") for a in r.resumen_comunidad.alertas_soporte)
    assert not any("Todos los registros" in a for a in r.resumen_comunidad.alertas_soporte)


def test_fallo_de_esquema_reintenta_a_temperatura_cero_preservando_base_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ca.UnexpectedModelBehavior("esquema inválido")
    # Mitigación Riesgo 1: si el agente tiene ajustes de caché (p. ej. Anthropic),
    # el reintento a temperatura 0.0 debe fusionarlos y no sobrescribirlos.
    agente = _AgenteFalso(
        fallos=1,
        error=error,
        model_settings={"anthropic_cache_instructions": "5m"},
    )
    _usar_agente(monkeypatch, agente)
    asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="anthropic"))
    assert agente.settings == [
        None,
        {"anthropic_cache_instructions": "5m", "temperature": 0.0},
    ]


def test_timeout_por_inferencia_activa_reintento_y_degrada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mitigación Riesgo 4: una llamada colgada debe cortar por timeout y degradar."""

    class _AgenteColgado:
        llamadas = 0

        async def run(self, entrada: str, model_settings: dict[str, Any] | None = None) -> Any:
            self.llamadas += 1
            await asyncio.Future()  # Se queda esperando indefinidamente

    agente = _AgenteColgado()
    _usar_agente(monkeypatch, agente)
    r = asyncio.run(
        ca.analizar_lote_cognitivo(
            [_interaccion(1)], "r", "q", provider="openai", timeout_seg=0.01
        )
    )
    assert agente.llamadas == ca.MAX_REINTENTOS_DEFECTO + 1
    assert ca.es_mensaje_degradado(r.mensajes_analizados[0])
    assert "TimeoutError" in r.mensajes_analizados[0].motivo_seleccion


def test_resolucion_de_proveedor_concurrencia_y_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "GEMINI")
    assert ca._resolver_proveedor(None) == "gemini"
    assert ca._resolver_proveedor("OpenAI") == "openai"
    monkeypatch.setenv("COGNITIVE_ANALYZER_CONCURRENCY", "basura")
    assert ca._leer_entero_env("COGNITIVE_ANALYZER_CONCURRENCY", 5) == 5
    monkeypatch.setenv("COGNITIVE_ANALYZER_CONCURRENCY", "8")
    assert ca._leer_entero_env("COGNITIVE_ANALYZER_CONCURRENCY", 5) == 8
    monkeypatch.setenv("LLM_INFERENCE_TIMEOUT_SEC", "-5")
    assert ca._leer_flotante_env("LLM_INFERENCE_TIMEOUT_SEC", 30.0) == 30.0
    monkeypatch.setenv("LLM_INFERENCE_TIMEOUT_SEC", "15.5")
    assert ca._leer_flotante_env("LLM_INFERENCE_TIMEOUT_SEC", 30.0) == 15.5


def test_lote_vacio_es_error() -> None:
    with pytest.raises(ValueError):
        asyncio.run(ca.analizar_lote_cognitivo([], "r", "q"))


def test_system_prompt_personalizado_se_propaga_al_agente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capturado: dict[str, Any] = {}

    def _factory_falsa(provider: str, system_prompt: str, result_schema: Any) -> _AgenteFalso:
        capturado["system_prompt"] = system_prompt
        return _AgenteFalso()

    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", _factory_falsa)
    asyncio.run(
        ca.analizar_lote_cognitivo(
            [_interaccion(1)], "r", "q", provider="openai", system_prompt="PROMPT_DE_PRUEBA"
        )
    )
    assert capturado["system_prompt"] == "PROMPT_DE_PRUEBA"


def test_system_prompt_por_defecto_usa_system_prompt_de_produccion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capturado: dict[str, Any] = {}

    def _factory_falsa(provider: str, system_prompt: str, result_schema: Any) -> _AgenteFalso:
        capturado["system_prompt"] = system_prompt
        return _AgenteFalso()

    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", _factory_falsa)
    asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="openai"))
    assert capturado["system_prompt"] == ca.SYSTEM_PROMPT


class _UsoFalso:
    """Imita los campos normalizados de pydantic_ai.usage.RunUsage."""

    def __init__(self, entrada: int, salida: int, cacheados: int = 0):
        self.input_tokens = entrada
        self.output_tokens = salida
        self.cache_read_tokens = cacheados


class _ResultadoConUso:
    def __init__(self, output: Any, uso: _UsoFalso):
        self.output = output
        self._uso = uso

    def usage(self) -> _UsoFalso:
        return self._uso


class _ModeloFalso:
    model_name = "modelo-de-prueba-1"


class _AgenteConUso:
    """Responde con un uso de tokens fijo y expone `.model.model_name`."""

    model = _ModeloFalso()

    def __init__(self, entrada: int = 100, salida: int = 20, cacheados: int = 30):
        self.uso = _UsoFalso(entrada, salida, cacheados)

    async def run(
        self, entrada: str, model_settings: dict[str, Any] | None = None
    ) -> _ResultadoConUso:
        return _ResultadoConUso(_mensaje("id_inventado_por_el_llm"), self.uso)


def test_telemetria_suma_tokens_de_todas_las_inferencias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _usar_agente(monkeypatch, _AgenteConUso(entrada=100, salida=20, cacheados=30))
    lote = [_interaccion(1), _interaccion(2), _interaccion(3)]
    analisis, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria(lote, "r", "q", provider="openai")
    )
    assert len(analisis.mensajes_analizados) == 3
    assert tel.tokens_entrada == 300 and tel.tokens_salida == 60
    assert tel.tokens_totales == 360
    assert tel.tokens_cacheados == 90
    assert tel.total_procesados == 3 and tel.degradados == 0


def test_telemetria_reporta_modelo_y_version_de_prompt_de_produccion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _usar_agente(monkeypatch, _AgenteConUso())
    _, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria([_interaccion(1)], "r", "q", provider="gemini")
    )
    assert tel.modelo == "modelo-de-prueba-1"
    assert tel.proveedor == "gemini"
    assert tel.prompt_version == PROMPT_VERSION
    assert isinstance(tel.latencia_ms, int) and tel.latencia_ms >= 0


def test_telemetria_marca_como_personalizado_un_prompt_distinto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _usar_agente(monkeypatch, _AgenteConUso())
    _, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria(
            [_interaccion(1)], "r", "q", provider="openai", system_prompt="PROMPT_DE_PRUEBA"
        )
    )
    assert tel.prompt_version.startswith("personalizado+")
    assert tel.prompt_version != PROMPT_VERSION


def test_version_de_prompt_cambia_si_cambia_el_contenido() -> None:
    assert version_de_prompt(ca.SYSTEM_PROMPT) == PROMPT_VERSION
    assert version_de_prompt("a") != version_de_prompt("b")
    assert version_de_prompt("a") == version_de_prompt("a")


def test_telemetria_modelo_desconocido_usa_valor_de_respaldo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _usar_agente(monkeypatch, _AgenteFalso())  # sin atributo `.model`
    _, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria([_interaccion(1)], "r", "q", provider="openai")
    )
    assert tel.modelo == "openai:desconocido"
    assert tel.tokens_totales == 0  # el uso de este agente falso no trae contadores


def test_telemetria_degradados_no_aportan_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _usar_agente(monkeypatch, _AgenteFalso(fallos=999))
    _, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria(
            [_interaccion(1), _interaccion(2)], "r", "q", provider="openai"
        )
    )
    assert tel.degradados == 2
    assert tel.tokens_entrada == 0 and tel.tokens_salida == 0 and tel.tokens_cacheados == 0


def test_telemetria_se_convierte_a_metadatos_ejecucion_valido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _usar_agente(monkeypatch, _AgenteConUso(entrada=50, salida=10))
    _, tel = asyncio.run(
        ca.analizar_lote_cognitivo_con_telemetria([_interaccion(1)], "r", "q", provider="openai")
    )
    meta = tel.a_metadatos_ejecucion()
    assert isinstance(meta, MetadatosEjecucion)
    assert meta.modelo == "modelo-de-prueba-1"
    assert meta.prompt_version == PROMPT_VERSION
    assert meta.tokens_totales == 60
    assert meta.latencia_ms == tel.latencia_ms


def test_firma_publica_de_analizar_lote_cognitivo_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conserva los 6 parámetros originales más el parámetro opcional `timeout_seg`."""
    assert list(inspect.signature(ca.analizar_lote_cognitivo).parameters) == [
        "interacciones",
        "run_id",
        "request_id",
        "provider",
        "max_concurrency",
        "system_prompt",
        "timeout_seg",
    ]
    _usar_agente(monkeypatch, _AgenteConUso())
    r = asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="openai"))
    assert isinstance(r, AnalisisCognitivo)


def test_la_variante_con_telemetria_valida_lote_vacio() -> None:
    with pytest.raises(ValueError):
        asyncio.run(ca.analizar_lote_cognitivo_con_telemetria([], "r", "q"))