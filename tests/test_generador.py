"""Pruebas de `ai_modules/generador.py` con agentes simulados (sin red ni saldo)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import ai_modules.generador as gen
from ai_modules.cognitive_analyzer import _calcular_resumen_comunidad
from ai_modules.generador import (
    GeneracionNoDisponibleError,
    construir_material,
    generar_activos_marketing,
)
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadataOrigenValidado,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenSemanal,
)


def _interaccion(id_: str, texto: str) -> InteraccionValidada:
    return InteraccionValidada(
        id=id_, autor="ana", canal="general", tipo="mensaje", texto=texto,
        metadata_origen=MetadataOrigenValidado(
            plataforma="Discord", identificador_original=id_, fecha="2026-10-12T10:00:00Z"
        ),
    )


def _mensaje(source_id: str, apto: bool, categoria: str = "Logro") -> MensajeAnalizado:
    return MensajeAnalizado(
        source_id=source_id, sentimiento="Positivo", categoria_enrutamiento=categoria,
        regla_aplicada="regla de prueba", temas=["python"],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2),
        motivo_seleccion="Motivo objetivo de la puntuación", apto_para_publicacion=apto,
    )


@pytest.fixture
def lote():
    interacciones = [
        _interaccion("m1", "Desplegué mi API, escríbanme a ana@correo.com"),
        _interaccion("m2", "No entiendo los decoradores"),
    ]
    mensajes = [_mensaje("m1", True), _mensaje("m2", False, "Duda")]
    analisis = AnalisisCognitivo(
        run_id="run-1", request_id="req-1",
        resumen_comunidad=_calcular_resumen_comunidad(mensajes, 2),
        mensajes_analizados=mensajes,
    )
    return analisis, interacciones


class AgenteFalso:
    def __init__(self, esquema, fallos_iniciales: int = 0):
        self.esquema = esquema
        self.model = SimpleNamespace(model_name="modelo-falso")
        self.model_settings = {"anthropic_cache_instructions": "5m"}
        self.fallos_iniciales = fallos_iniciales
        self.llamadas: list[dict] = []

    async def run(self, entrada, model_settings=None):
        self.llamadas.append({"entrada": entrada, "settings": model_settings})
        if len(self.llamadas) <= self.fallos_iniciales:
            raise ConnectionError("proveedor caído")
        if self.esquema is PostLinkedIn:
            salida = PostLinkedIn(titulo="Titulo ok", cuerpo="c" * 25, source_ids=["m1"])
        else:
            salida = ResumenSemanal(seccion="s", titular="Titular ok", resumen="r" * 25, source_ids=["m1"])
        return SimpleNamespace(
            output=salida, usage=lambda: SimpleNamespace(input_tokens=5, output_tokens=7)
        )


@pytest.fixture
def agentes(monkeypatch):
    creados: dict[str, AgenteFalso] = {}

    def fabricar(proveedor, prompt, esquema):
        agente = AgenteFalso(esquema, fallos_iniciales=getattr(fabricar, "fallos", 0))
        creados[esquema.__name__] = agente
        return agente

    monkeypatch.setattr(gen.CognitiveAgentFactory, "create_agent", staticmethod(fabricar))
    monkeypatch.setattr(gen, "_espera_backoff", lambda _intento: 0.0)
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    fabricar.creados = creados
    return fabricar


def test_material_solo_incluye_mensajes_aptos_y_enmascara_pii(lote):
    analisis, interacciones = lote
    material = construir_material(analisis, interacciones, "Logro")

    assert material["source_ids_permitidos"] == ["m1"]
    assert [m["source_id"] for m in material["mensajes"]] == ["m1"]
    texto = json.dumps(material, ensure_ascii=False)
    assert "ana@correo.com" not in texto and "[EMAIL_PROTEGIDO]" in texto
    assert "decoradores" not in texto  # el no apto nunca llega al LLM
    assert material["instruccion_de_tono"]


def test_genera_ambos_formatos_y_suma_la_telemetria(lote, agentes):
    analisis, interacciones = lote

    post, resumen, tel = asyncio.run(
        generar_activos_marketing(analisis, interacciones, "Logro", "run-1", "req-1", provider="openai")
    )

    assert isinstance(post, PostLinkedIn) and isinstance(resumen, ResumenSemanal)
    assert set(agentes.creados) == {"PostLinkedIn", "ResumenSemanal"}  # M01: siempre ambos
    assert tel.modelo == "modelo-falso"
    assert (tel.tokens_entrada, tel.tokens_salida, tel.tokens_totales) == (10, 14, 24)
    assert tel.prompt_version.startswith("generador-")
    assert tel.a_metadatos_ejecucion().tokens_totales == 24
    # Ambos agentes recibieron el mismo material.
    entradas = {a.llamadas[0]["entrada"] for a in agentes.creados.values()}
    assert len(entradas) == 1


def test_temperatura_cero_conserva_los_ajustes_base_del_agente(lote, agentes):
    analisis, interacciones = lote

    asyncio.run(generar_activos_marketing(analisis, interacciones, "Logro", "r", "q", temperatura_cero=True))
    asyncio.run(generar_activos_marketing(analisis, interacciones, "Logro", "r", "q"))

    primera = agentes.creados["PostLinkedIn"].llamadas[0]["settings"]
    assert primera is None or primera == {"anthropic_cache_instructions": "5m", "temperature": 0.0}


def test_reintenta_ante_fallos_transitorios(lote, agentes):
    analisis, interacciones = lote
    agentes.fallos = 1  # el primer intento de cada agente falla

    post, _resumen, _tel = asyncio.run(
        generar_activos_marketing(analisis, interacciones, "Logro", "r", "q")
    )

    assert post.source_ids == ["m1"]
    assert len(agentes.creados["PostLinkedIn"].llamadas) == 2


def test_error_claro_cuando_el_proveedor_agota_los_reintentos(lote, agentes):
    analisis, interacciones = lote
    agentes.fallos = 99

    with pytest.raises(GeneracionNoDisponibleError, match="ConnectionError"):
        asyncio.run(generar_activos_marketing(analisis, interacciones, "Logro", "r", "q"))


def test_sin_mensajes_aptos_no_genera(agentes):
    mensajes = [_mensaje("m2", False, "Duda")]
    analisis = AnalisisCognitivo(
        run_id="r", request_id="q",
        resumen_comunidad=_calcular_resumen_comunidad(mensajes, 1), mensajes_analizados=mensajes,
    )
    with pytest.raises(ValueError, match="al menos un mensaje apto"):
        asyncio.run(generar_activos_marketing(analisis, [_interaccion("m2", "hola")], "Duda", "r", "q"))
    assert agentes.creados == {}
