"""Regresiones para preservar la API de negocio del generador original."""
import pytest

import ai_modules.generador as generador
from test_generador import _analisis, _interaccion, _mensaje


def test_api_original_se_conserva_en_generador_integrado():
    for nombre in (
        "buscar_interaccion_por_source_id",
        "generar_post_linkedin",
        "registrar_no_publicable",
        "generar_resumen_semanal",
        "generar_activos",
        "construir_paquete_salida",
        "generar_activos_marketing",
    ):
        assert callable(getattr(generador, nombre))


def test_generador_determinista_conserva_casos_no_publicables_sin_filtrar_texto():
    texto_privado = "MENSAJE PRIVADO QUE NUNCA DEBE APARECER EN EL RESUMEN"
    interacciones = [
        _interaccion("pub", "Aprobé mi evaluación de OCI"),
        _interaccion("priv", texto_privado),
    ]
    analisis = _analisis([
        _mensaje("pub", temas=["OCI"], publicable=True),
        _mensaje("priv", categoria="Dificultad", publicable=False, temas=["soporte"]),
    ])

    activos = generador.generar_activos(interacciones, analisis.mensajes_analizados)

    assert activos.post_linkedin.source_ids == ["pub"]
    assert set(activos.resumen_semanal.source_ids) == {"pub", "priv"}
    assert [caso.source_id for caso in activos.casos_no_publicables] == ["priv"]
    assert texto_privado not in activos.resumen_semanal.resumen
    assert len(activos.post_linkedin.cuerpo) >= 20
    assert len(activos.resumen_semanal.resumen) >= 20


def test_generador_determinista_rechaza_source_id_no_coincidente():
    interaccion = _interaccion("original", "Aprobé mi evaluación de OCI")
    mensaje = _mensaje("otro-id", publicable=True)
    with pytest.raises(ValueError, match="no corresponden al mismo source_id"):
        generador.generar_post_linkedin(interaccion, mensaje)


def test_resumen_determinista_no_asigna_source_id_sin_interaccion():
    interacciones = [_interaccion("pub", "Aprobé mi evaluación de OCI")]
    analisis = [
        _mensaje("pub", temas=["OCI"], publicable=True),
        _mensaje("faltante", temas=["python"], publicable=True),
    ]
    resumen, casos = generador.generar_resumen_semanal(interacciones, analisis)
    assert resumen.source_ids == ["pub"]
    assert casos == []
