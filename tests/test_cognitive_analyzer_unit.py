"""Pruebas unitarias aisladas (sin API externa) de ai_modules/cognitive_analyzer.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ai_modules import cognitive_analyzer as ca
from esquemas import InteraccionValidada, MensajeAnalizado, PuntuacionRelevancia


def _interaccion(i: int, texto: str = "Terminé el módulo de LangGraph hoy") -> InteraccionValidada:
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
    def __init__(self, output):
        self.output = output

    def usage(self):
        return object()


class _AgenteFalso:
    """Falla `fallos` veces con `error` y luego responde con source_id ajeno (a propósito)."""

    def __init__(self, fallos: int = 0, error: Exception | None = None):
        self.llamadas = 0
        self.fallos = fallos
        self.error = error or RuntimeError("503")
        self.settings: list = []

    async def run(self, entrada, model_settings=None):
        self.llamadas += 1
        self.settings.append(model_settings)
        if self.llamadas <= self.fallos:
            raise self.error
        return _Resultado(_mensaje("id_inventado_por_el_llm"))


class _AgenteSegunContenido:
    """Decide su respuesta según qué `source_id` aparece en la entrada
    serializada, no según el orden de llamada — las interacciones se
    procesan de forma concurrente vía `asyncio.gather`, así que depender
    del orden de llegada sería una prueba frágil."""

    def __init__(self, ids_que_fallan: set[str], error: Exception | None = None):
        self.ids_que_fallan = ids_que_fallan
        self.error = error or RuntimeError("503")
        self.llamadas = 0

    async def run(self, entrada, model_settings=None):
        self.llamadas += 1
        if any(f'"id":"{id_}"' in entrada or f'"id": "{id_}"' in entrada for id_ in self.ids_que_fallan):
            raise self.error
        return _Resultado(_mensaje("id_inventado_por_el_llm"))


@pytest.fixture(autouse=True)
def _sin_espera(monkeypatch):
    async def _nada(_):
        return None

    monkeypatch.setattr(ca.asyncio, "sleep", _nada)


def _usar_agente(monkeypatch, agente) -> None:
    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", lambda *a, **k: agente)


def test_enmascara_pii():
    t = ca._enmascarar_pii("Escríbeme a ana@mail.com o al +52 55 1234 5678, api_key=abc123")
    assert "ana@mail.com" not in t and "1234 5678" not in t and "abc123" not in t
    assert "[EMAIL_PROTEGIDO]" in t and "[TELEFONO_PROTEGIDO]" in t


def test_truncamiento_conserva_proporcion_20_80():
    corto = "x" * 100
    assert ca._truncar_texto_semantico(corto) == corto
    largo = "A" * 3000 + "Z" * 3000
    r = ca._truncar_texto_semantico(largo, max_chars=1000)
    assert r.startswith("A" * 200) and r.endswith("Z" * 800)
    assert "truncado" in r


def test_resumen_determinista():
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


def test_resumen_detecta_empate_y_reporta_mixto():
    """CORRECCIÓN: `max(conteo, key=...)` resolvía cualquier empate a una
    sola categoría arbitraria. El esquema documenta "Mixto" explícitamente
    para este caso (`ResumenComunidad.sentimiento_predominante`)."""
    msgs = [
        _mensaje("a", "Positivo"),
        _mensaje("b", "Dificultad/Negativo"),
    ]
    r = ca._calcular_resumen_comunidad(msgs, 2)
    assert r.sentimiento_predominante == "Mixto"


def test_resumen_sin_empate_no_reporta_mixto():
    msgs = [
        _mensaje("a", "Positivo"),
        _mensaje("b", "Positivo"),
        _mensaje("c", "Neutro"),
    ]
    r = ca._calcular_resumen_comunidad(msgs, 3)
    assert r.sentimiento_predominante == "Positivo"


def test_degradado_preserva_source_id_y_score_cero():
    m = ca._crear_mensaje_degradado("msg_009", RuntimeError("boom"))
    assert m.source_id == "msg_009"
    assert m.puntuacion_relevancia.total == 0 and not m.apto_para_publicacion
    assert "RuntimeError" in m.motivo_seleccion and "boom" not in m.motivo_seleccion
    # CORRECCIÓN: antes requiere_soporte=False dejaba la degradación
    # invisible en alertas_soporte salvo que degradara el 100% del lote.
    assert m.requiere_soporte is True
    assert m.categoria_enrutamiento == "Dificultad"


def test_flujo_nominal_fuerza_source_id_original(monkeypatch):
    _usar_agente(monkeypatch, _AgenteFalso())
    lote = [_interaccion(1), _interaccion(2)]
    r = asyncio.run(ca.analizar_lote_cognitivo(lote, "run-1", "req-1", provider="openai"))
    assert [m.source_id for m in r.mensajes_analizados] == ["msg_001", "msg_002"]
    assert r.run_id == "run-1" and r.request_id == "req-1"


def test_reintento_transitorio_se_recupera(monkeypatch):
    agente = _AgenteFalso(fallos=2)
    _usar_agente(monkeypatch, agente)
    r = asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="gemini"))
    assert agente.llamadas == 3
    assert r.resumen_comunidad.registros_rechazados == 0


def test_agotamiento_degrada_sin_romper_lote(monkeypatch):
    agente = _AgenteFalso(fallos=999)
    _usar_agente(monkeypatch, agente)
    lote = [_interaccion(1), _interaccion(2)]
    r = asyncio.run(ca.analizar_lote_cognitivo(lote, "r", "q", provider="anthropic"))
    assert agente.llamadas == 2 * (ca.MAX_REINTENTOS_DEFECTO + 1)
    assert [m.source_id for m in r.mensajes_analizados] == ["msg_001", "msg_002"]
    assert all(m.puntuacion_relevancia.total == 0 for m in r.mensajes_analizados)
    assert r.resumen_comunidad.registros_validos == 1
    assert any("degradados" in a for a in r.resumen_comunidad.alertas_soporte)


def test_degradacion_parcial_queda_visible_en_alertas_soporte(monkeypatch):
    """Caso realista: solo uno de dos mensajes degrada. Antes de la
    corrección, ese mensaje no aparecía en alertas_soporte porque el
    fallback genérico solo se dispara cuando el 100% del lote degrada."""
    agente = _AgenteSegunContenido(ids_que_fallan={"msg_001"})
    _usar_agente(monkeypatch, agente)
    lote = [_interaccion(1), _interaccion(2)]
    r = asyncio.run(ca.analizar_lote_cognitivo(lote, "r", "q", provider="openai"))

    assert r.resumen_comunidad.registros_validos == 1
    assert r.resumen_comunidad.registros_rechazados == 1
    # El mensaje degradado debe quedar localizable por source_id, no solo
    # contabilizado en un número agregado.
    assert any(a.startswith("[msg_001]") for a in r.resumen_comunidad.alertas_soporte)
    assert not any("Todos los registros" in a for a in r.resumen_comunidad.alertas_soporte)


def test_fallo_de_esquema_reintenta_a_temperatura_cero(monkeypatch):
    error = ca.UnexpectedModelBehavior("esquema inválido")
    agente = _AgenteFalso(fallos=1, error=error)
    _usar_agente(monkeypatch, agente)
    asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="openai"))
    assert agente.settings == [None, {"temperature": 0.0}]


def test_resolucion_de_proveedor_y_concurrencia(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "GEMINI")
    assert ca._resolver_proveedor(None) == "gemini"
    assert ca._resolver_proveedor("OpenAI") == "openai"
    monkeypatch.setenv("COGNITIVE_ANALYZER_CONCURRENCY", "basura")
    assert ca._leer_entero_env("COGNITIVE_ANALYZER_CONCURRENCY", 5) == 5
    monkeypatch.setenv("COGNITIVE_ANALYZER_CONCURRENCY", "8")
    assert ca._leer_entero_env("COGNITIVE_ANALYZER_CONCURRENCY", 5) == 8


def test_lote_vacio_es_error():
    with pytest.raises(ValueError):
        asyncio.run(ca.analizar_lote_cognitivo([], "r", "q"))


def test_system_prompt_personalizado_se_propaga_al_agente(monkeypatch):
    """`system_prompt` debe llegar intacto a CognitiveAgentFactory.create_agent
    — es lo que permite al arnés de calibración ejecutar la Condición A
    (zero-shot) y la Condición B (few-shot) contra la misma ruta real de
    inferencia, sin reconstruir su propia lógica de invocación."""
    capturado: dict = {}

    def _factory_falsa(provider, system_prompt, result_schema):
        capturado["system_prompt"] = system_prompt
        return _AgenteFalso()

    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", _factory_falsa)
    asyncio.run(
        ca.analizar_lote_cognitivo(
            [_interaccion(1)], "r", "q", provider="openai", system_prompt="PROMPT_DE_PRUEBA"
        )
    )
    assert capturado["system_prompt"] == "PROMPT_DE_PRUEBA"


def test_system_prompt_por_defecto_usa_system_prompt_de_produccion(monkeypatch):
    capturado: dict = {}

    def _factory_falsa(provider, system_prompt, result_schema):
        capturado["system_prompt"] = system_prompt
        return _AgenteFalso()

    monkeypatch.setattr(ca.CognitiveAgentFactory, "create_agent", _factory_falsa)
    asyncio.run(ca.analizar_lote_cognitivo([_interaccion(1)], "r", "q", provider="openai"))
    assert capturado["system_prompt"] == ca.SYSTEM_PROMPT
