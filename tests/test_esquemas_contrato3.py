"""Pruebas del contrato 3 (esquemas.py): `CasoNoPublicable` y
`ActivosGenerados.casos_no_publicables`.

Cubren tres cosas:
1. Compatibilidad hacia atrás: un `PaqueteSalida` sin casos no publicables
   sigue siendo válido (el campo nuevo es opcional con lista vacía).
2. El tipado estricto del campo nuevo (mismos `Literal` que `MensajeAnalizado`)
   y su construcción determinista desde un `MensajeAnalizado`.
3. Una guardia contra una regresión detectada en una propuesta tentativa de
   `esquemas.py`: `PuntuacionRelevancia.total` debe seguir calculándose solo
   (`calcular_total`); si volviera a ser obligatorio, el fallback de
   degradación de `cognitive_analyzer.py` y toda salida del LLM fallarían.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from esquemas import (
    ActivosGenerados,
    CasoNoPublicable,
    EvidenciaAlmacenamientoOCI,
    MensajeAnalizado,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)


def _mensaje(
    source_id: str = "msg_001",
    apto: bool = False,
    sentimiento: str = "Dificultad/Negativo",
    categoria: str = "Dificultad",
    soporte: bool = True,
) -> MensajeAnalizado:
    return MensajeAnalizado(
        source_id=source_id,
        sentimiento=sentimiento,  # type: ignore[arg-type]
        categoria_enrutamiento=categoria,  # type: ignore[arg-type]
        regla_aplicada="regla de prueba",
        temas=["LangGraph", "Streamlit"],
        puntuacion_relevancia=PuntuacionRelevancia(
            evidencia_explicita=1, utilidad_comunitaria=0, claridad_contexto=1
        ),
        motivo_seleccion="Justificación suficiente para la prueba.",
        requiere_soporte=soporte,
        apto_para_publicacion=apto,
    )


def _activos(casos: list[CasoNoPublicable] | None = None) -> ActivosGenerados:
    kwargs = {} if casos is None else {"casos_no_publicables": casos}
    return ActivosGenerados(
        post_linkedin=PostLinkedIn(
            titulo="Titulo de prueba",
            cuerpo="Cuerpo de la publicación con longitud suficiente.",
            source_ids=["msg_002"],
        ),
        resumen_semanal=ResumenSemanal(
            seccion="Comunidad",
            titular="Titular de prueba",
            resumen="Resumen semanal con longitud suficiente para validar.",
            source_ids=["msg_001", "msg_002"],
        ),
        **kwargs,
    )


def _paquete(activos: ActivosGenerados) -> PaqueteSalida:
    return PaqueteSalida(
        run_id="run-1",
        request_id="req-1",
        metadatos_ejecucion=MetadatosEjecucion(
            modelo="modelo-prueba", prompt_version="v1", latencia_ms=10, tokens_totales=100
        ),
        analisis_resumido=ResumenComunidad(
            total_interacciones_procesadas=2,
            registros_validos=2,
            sentimiento_predominante="Mixto",
            distribucion_sentimiento={
                "Altamente Positivo": 0,
                "Positivo": 1,
                "Neutro": 0,
                "Dificultad/Negativo": 1,
            },
            temas_principales=["LangGraph"],
        ),
        activos=activos,
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            ruta_objeto="activos/2026-W42/run-1/revision-001.json"
        ),
    )


# --- 1. compatibilidad hacia atrás ----------------------------------------


def test_activos_sin_casos_no_publicables_sigue_siendo_valido():
    activos = _activos()
    assert activos.casos_no_publicables == []
    assert _paquete(activos).activos.casos_no_publicables == []


def test_listas_por_defecto_no_se_comparten_entre_instancias():
    a, b = _activos(), _activos()
    a.casos_no_publicables.append(CasoNoPublicable.desde_mensaje_analizado(_mensaje()))
    assert b.casos_no_publicables == []


def test_paquete_con_casos_no_publicables_hace_ida_y_vuelta_json():
    caso = CasoNoPublicable.desde_mensaje_analizado(_mensaje())
    paquete = _paquete(_activos([caso]))
    reconstruido = PaqueteSalida.model_validate_json(paquete.model_dump_json())
    assert reconstruido == paquete
    assert reconstruido.activos.casos_no_publicables[0].source_id == "msg_001"


def test_json_antiguo_sin_el_campo_nuevo_sigue_validando():
    datos = _paquete(_activos()).model_dump(mode="json")
    datos["activos"].pop("casos_no_publicables")
    assert PaqueteSalida.model_validate(datos).activos.casos_no_publicables == []


# --- 2. tipado estricto y construcción determinista ------------------------


def test_caso_no_publicable_rechaza_sentimiento_fuera_del_contrato():
    with pytest.raises(ValidationError):
        CasoNoPublicable(
            source_id="x",
            sentimiento="Enojado",  # type: ignore[arg-type]
            categoria_enrutamiento="Dificultad",
            temas=["a"],
            motivo_seleccion="Justificación suficiente.",
        )


def test_caso_no_publicable_rechaza_categoria_fuera_del_contrato():
    with pytest.raises(ValidationError):
        CasoNoPublicable(
            source_id="x",
            sentimiento="Neutro",
            categoria_enrutamiento="Queja",  # type: ignore[arg-type]
            temas=["a"],
            motivo_seleccion="Justificación suficiente.",
        )


def test_caso_no_publicable_exige_temas_y_motivo_como_mensaje_analizado():
    with pytest.raises(ValidationError):
        CasoNoPublicable(
            source_id="x",
            sentimiento="Neutro",
            categoria_enrutamiento="Duda",
            temas=[],
            motivo_seleccion="Justificación suficiente.",
        )
    with pytest.raises(ValidationError):
        CasoNoPublicable(
            source_id="x",
            sentimiento="Neutro",
            categoria_enrutamiento="Duda",
            temas=["a"],
            motivo_seleccion="corto",
        )


def test_valores_permitidos_coinciden_con_los_de_mensaje_analizado():
    """Los `Literal` se comparten por alias; este test falla si alguien vuelve
    a duplicarlos y los desincroniza."""

    def valores(modelo, campo):
        return modelo.model_json_schema()["properties"][campo]["enum"]

    for campo in ("sentimiento", "categoria_enrutamiento"):
        assert valores(CasoNoPublicable, campo) == valores(MensajeAnalizado, campo)


def test_desde_mensaje_analizado_copia_los_campos_sin_alterarlos():
    mensaje = _mensaje(source_id="msg_007", sentimiento="Neutro", categoria="Duda", soporte=False)
    caso = CasoNoPublicable.desde_mensaje_analizado(mensaje)
    assert caso.source_id == "msg_007"
    assert caso.sentimiento == "Neutro"
    assert caso.categoria_enrutamiento == "Duda"
    assert caso.temas == ["LangGraph", "Streamlit"]
    assert caso.requiere_soporte is False
    assert caso.motivo_seleccion == mensaje.motivo_seleccion


def test_desde_mensaje_analizado_copia_temas_sin_compartir_la_lista():
    mensaje = _mensaje()
    caso = CasoNoPublicable.desde_mensaje_analizado(mensaje)
    caso.temas.append("otro")
    assert mensaje.temas == ["LangGraph", "Streamlit"]


def test_desde_mensaje_analizado_rechaza_mensajes_aptos():
    with pytest.raises(ValueError, match="apto para publicación"):
        CasoNoPublicable.desde_mensaje_analizado(_mensaje(apto=True))


# --- 3. guardia contra la regresión de `total` ------------------------------


def test_puntuacion_relevancia_calcula_total_sin_recibirlo():
    p = PuntuacionRelevancia(evidencia_explicita=2, utilidad_comunitaria=1, claridad_contexto=1)
    assert p.total == 4


def test_puntuacion_relevancia_descarta_un_total_incorrecto_del_llm():
    p = PuntuacionRelevancia(
        evidencia_explicita=1, utilidad_comunitaria=1, claridad_contexto=1, total=6
    )
    assert p.total == 3
