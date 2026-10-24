"""Pruebas de contrato del generador de activos de marketing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import ai_modules.generador as generador
from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadataOrigenValidado,
    MetadatosEjecucion,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)


def _interaccion(identifier: str, texto: str) -> InteraccionValidada:
    return InteraccionValidada(
        id=identifier,
        autor="participante",
        canal="Discord",
        tipo="mensaje",
        texto=texto,
        metadata_origen=MetadataOrigenValidado(
            plataforma="Discord",
            identificador_original=identifier,
            fecha=datetime(2026, 10, 21, tzinfo=timezone.utc),
        ),
    )


def _mensaje(
    identifier: str,
    *,
    categoria: str = "Logro",
    publicable: bool = True,
    score: int = 3,
    temas: list[str] | None = None,
) -> MensajeAnalizado:
    temas = temas or ["OCI"]
    return MensajeAnalizado(
        source_id=identifier,
        sentimiento="Positivo" if publicable else "Dificultad/Negativo",
        categoria_enrutamiento=categoria,
        regla_aplicada="Clasificación basada en el contenido de la interacción.",
        temas=temas,
        entidades_relevantes=[],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=score // 3,
            utilidad_comunitaria=min(2, score // 2),
            claridad_contexto=min(2, score - score // 3 - min(2, score // 2)),
        ),
        motivo_seleccion="El contenido aporta contexto comunitario verificable.",
        requiere_soporte=not publicable,
        apto_para_publicacion=publicable,
    )


def _analisis(mensajes: list[MensajeAnalizado]) -> AnalisisCognitivo:
    return AnalisisCognitivo(
        run_id="run-test",
        request_id="req-test",
        resumen_comunidad=ResumenComunidad(
            total_interacciones_procesadas=len(mensajes),
            registros_validos=len(mensajes),
            registros_rechazados=0,
            sentimiento_predominante="Mixto",
            distribucion_sentimiento={"Positivo": 1},
            temas_principales=["OCI"],
            alertas_soporte=[],
        ),
        mensajes_analizados=mensajes,
    )


class _FakeAgent:
    def __init__(self, output_type, prompts: list[str], fail: bool = False, hang: bool = False):
        self.output_type = output_type
        self.prompts = prompts
        self.fail = fail
        self.hang = hang
        self.model_settings = {}

    async def run(self, prompt: str, model_settings=None):
        self.prompts.append(prompt)
        if self.hang:
            await asyncio.Future()
        if self.fail:
            raise RuntimeError("simulated provider outage")
        if model_settings is not None:
            assert model_settings["temperature"] == 0.0
        if self.output_type is PostLinkedIn:
            output = PostLinkedIn(
                titulo="Comunidad aprende OCI",
                cuerpo="La comunidad compartió un avance documentado sobre OCI.",
                hashtags=["#OCI"],
                source_ids=["id-inventado"],
            )
        else:
            output = ResumenSemanal(
                seccion="Actividad",
                titular="Resumen semanal de la comunidad",
                resumen="Se observaron temas de aprendizaje y actividad comunitaria.",
                source_ids=["id-inventado"],
            )
        return SimpleNamespace(
            output=output,
            usage={"input_tokens": 10, "output_tokens": 7, "cache_read_tokens": 2},
        )


class _FakeFactory:
    prompts: list[str] = []
    fail = False
    hang = False

    @classmethod
    def create_agent(cls, provider, system_prompt, output_type):
        return _FakeAgent(output_type, cls.prompts, fail=cls.fail, hang=cls.hang)

    @staticmethod
    def read_usage(result):
        return result.usage

    @staticmethod
    def extract_token_counts(usage):
        return usage["input_tokens"], usage["output_tokens"]

    @staticmethod
    def extract_cached_tokens(provider, usage):
        return usage["cache_read_tokens"]


def _install_fakes(monkeypatch, *, fail=False, hang=False):
    _FakeFactory.prompts = []
    _FakeFactory.fail = fail
    _FakeFactory.hang = hang
    monkeypatch.setattr(generador, "CognitiveAgentFactory", _FakeFactory)
    monkeypatch.setattr(generador, "resolver_modelo", lambda provider: "modelo-prueba")
    monkeypatch.setattr(generador, "_proveedor_activo", lambda: "gemini")


def test_selecciona_primero_enfoque_y_luego_relevancia():
    interacciones = [
        _interaccion("a", "Un mensaje de logro"),
        _interaccion("b", "Una pregunta técnica"),
    ]
    analisis = _analisis(
        [
            _mensaje("a", categoria="Logro", score=6),
            _mensaje("b", categoria="Duda", score=2),
        ]
    )
    seleccionada, mensaje = generador._seleccionar_fuente(analisis, interacciones, "Duda")
    assert seleccionada.id == "b"
    assert mensaje.source_id == "b"


def test_casos_no_publicables_no_exponen_texto_en_prompt_resumen():
    privado = "TEXTO PRIVADO QUE NO DEBE FILTRARSE"
    interacciones = [
        _interaccion("pub", "Aprobé mi evaluación OCI"),
        _interaccion("priv", privado),
    ]
    analisis = _analisis(
        [
            _mensaje("pub", publicable=True),
            _mensaje("priv", publicable=False, categoria="Dificultad", temas=["soporte", "OCI"]),
        ]
    )
    registros, ids = generador._registros_resumen(interacciones, analisis)
    assert ids == ["pub", "priv"]
    assert "texto" in registros[0]
    assert "texto" not in registros[1]
    assert privado not in repr(registros)


def test_enmascara_pii_en_prompts_y_plantillas_degradadas(monkeypatch):
    _install_fakes(monkeypatch, fail=True)
    interacciones = [
        _interaccion("pub", "Aprobé OCI ayer! Mi correo es ana@example.com y tel +52 55 1234 5678")
    ]
    analisis = _analisis([_mensaje("pub", temas=["OCI"])])
    post, resumen, _ = asyncio.run(
        generador.generar_activos_marketing(
            analisis, interacciones, "Logro", "run-pii", "req-pii"
        )
    )
    prompts_unidos = "\n".join(_FakeFactory.prompts)
    assert "ana@example.com" not in prompts_unidos
    assert "1234 5678" not in prompts_unidos
    assert "ana@example.com" not in post.cuerpo
    assert "[EMAIL_PROTEGIDO]" in post.cuerpo


def test_timeout_por_inferencia_degrada_a_plantilla_sin_bloquear(monkeypatch):
    _install_fakes(monkeypatch, hang=True)
    interacciones = [_interaccion("pub", "¡Aprobé OCI ayer!")]
    analisis = _analisis([_mensaje("pub", temas=["OCI"])])
    post, resumen, tel = asyncio.run(
        generador.generar_activos_marketing(
            analisis,
            interacciones,
            "Logro",
            "run-timeout",
            "req-timeout",
            timeout_seg=0.01,
        )
    )
    assert post.source_ids == ["pub"]
    assert resumen.source_ids == ["pub"]
    assert tel.tokens_totales == 0


def test_generacion_asigna_source_ids_verificados_y_suma_tokens(monkeypatch):
    _install_fakes(monkeypatch)
    interacciones = [_interaccion("real-1", "Aprobé mi evaluación OCI")]
    analisis = _analisis([_mensaje("real-1", temas=["OCI"])])
    post, resumen, telemetria = asyncio.run(
        generador.generar_activos_marketing(
            analisis, interacciones, "Logro", "run-1", "req-1", temperatura_cero=True
        )
    )
    assert post.source_ids == ["real-1"]
    assert resumen.source_ids == ["real-1"]
    assert telemetria.tokens_entrada == 20
    assert telemetria.tokens_salida == 14
    assert telemetria.tokens_totales == 34
    assert telemetria.tokens_cacheados == 4
    assert "sha256:" in telemetria.prompt_version
    assert len(_FakeFactory.prompts) == 2


def test_falla_del_proveedor_degrada_a_plantilla_sin_filtrar_privado(monkeypatch):
    _install_fakes(monkeypatch, fail=True)
    privado = "TEXTO ULTRAPRIVADO NO PUBLICAR"
    interacciones = [_interaccion("pub", "¡Aprobé OCI ayer!"), _interaccion("priv", privado)]
    analisis = _analisis(
        [
            _mensaje("pub", temas=["OCI"]),
            _mensaje("priv", categoria="Dificultad", publicable=False, temas=["soporte"]),
        ]
    )
    post, resumen, telemetria = asyncio.run(
        generador.generar_activos_marketing(
            analisis, interacciones, "Logro", "run-2", "req-2", temperatura_cero=False
        )
    )
    assert len(post.cuerpo) >= 20
    assert len(resumen.resumen) >= 20
    assert post.source_ids == ["pub"]
    assert resumen.source_ids == ["pub", "priv"]
    assert privado not in resumen.resumen
    assert privado not in "\n".join(_FakeFactory.prompts)
    assert telemetria.tokens_totales == 0


def test_texto_minimo_no_modifica_textos_que_ya_cumplen_longitud():
    texto = "Este cuerpo ya cumple con la longitud mínima."
    assert generador._texto_minimo(texto) == texto
    assert len(generador._texto_minimo("¡Aprobé OCI ayer!")) >= 20


def test_construir_paquete_salida_usa_revision_001():
    interacciones = [_interaccion("m1", "Logré configurar mi VCN en OCI exitosamente.")]
    analisis = _analisis([_mensaje("m1", temas=["OCI", "VCN"])])
    activos = generador.generar_activos(interacciones, analisis.mensajes_analizados)
    meta = MetadatosEjecucion(
        modelo="modelo-prueba", prompt_version="1.0.1", latencia_ms=10, tokens_totales=0
    )
    paquete = generador.construir_paquete_salida(analisis, activos, meta)
    assert paquete.almacenamiento_oci.ruta_objeto.endswith("/revision-001.json")