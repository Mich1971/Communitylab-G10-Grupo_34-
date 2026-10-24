import json
from collections import Counter
from pathlib import Path

from ai_modules.prompts import CATALOGO_FEW_SHOT, SYSTEM_PROMPT
from esquemas import InteraccionValidada, MensajeAnalizado

DATASET_VALIDACION = Path(__file__).parents[1] / "data" / "samples" / "validation_ground_truth.json"


def test_catalogo_few_shot_cumple_el_contrato():
    ejemplos = json.loads(CATALOGO_FEW_SHOT)

    assert len(ejemplos) == 17
    assert len({ejemplo["input"]["id"] for ejemplo in ejemplos}) == 17

    for ejemplo in ejemplos:
        entrada = InteraccionValidada.model_validate(ejemplo["input"])
        salida = MensajeAnalizado.model_validate(ejemplo["output"])
        puntuacion = ejemplo["output"]["puntuacion_relevancia"]

        assert entrada.id == salida.source_id

        # CORRECCIÓN: el catálogo ya no incluye "total" en puntuacion_relevancia
        # (ver instructions.py/catalog.py) porque esquemas.py lo recalcula de
        # forma determinista en PuntuacionRelevancia.calcular_total y pedírselo
        # también al LLM arriesgaba rechazar un registro válido por un campo
        # que de todas formas se iba a sobrescribir. La aserción anterior leía
        # puntuacion["total"] directamente del JSON crudo y ahora lanzaría
        # KeyError, porque esa clave ya no existe en los 17 ejemplos. Se
        # reemplaza por dos aserciones: (a) un guardia de regresión que
        # confirma que "total" sigue ausente del catálogo crudo, y (b) la
        # verificación real de que el validador de Pydantic —no el LLM—
        # calculó correctamente el total a partir de los tres subscores.
        assert "total" not in puntuacion, (
            "El catálogo no debe incluir 'total': esquemas.py lo calcula "
            "de forma determinista, nunca confiando en la aritmética del LLM."
        )
        assert salida.puntuacion_relevancia.total == sum(
            puntuacion[campo]
            for campo in ("evidencia_explicita", "utilidad_comunitaria", "claridad_contexto")
        )

    assert len(SYSTEM_PROMPT) > len(CATALOGO_FEW_SHOT)


def test_holdout_tiene_cuotas_contratos_y_es_disjunto_del_catalogo():
    ejemplos_catalogo = json.loads(CATALOGO_FEW_SHOT)
    ejemplos_validacion = json.loads(DATASET_VALIDACION.read_text(encoding="utf-8"))

    assert len(ejemplos_validacion) == 75
    assert Counter(ejemplo["expected_enrutamiento"] for ejemplo in ejemplos_validacion) == {
        "Logro": 24,
        "Duda": 26,
        "Dificultad": 25,
    }

    ids_catalogo = {ejemplo["input"]["id"] for ejemplo in ejemplos_catalogo}
    ids_validacion = {ejemplo["input"]["id"] for ejemplo in ejemplos_validacion}
    textos_catalogo = {ejemplo["input"]["texto"] for ejemplo in ejemplos_catalogo}
    textos_validacion = {ejemplo["input"]["texto"] for ejemplo in ejemplos_validacion}

    assert len(ids_validacion) == 75
    assert ids_catalogo.isdisjoint(ids_validacion)
    assert textos_catalogo.isdisjoint(textos_validacion)

    for ejemplo in ejemplos_validacion:
        InteraccionValidada.model_validate(ejemplo["input"])
        assert ejemplo["expected_sentimiento"] in {
            "Altamente Positivo",
            "Positivo",
            "Neutro",
            "Dificultad/Negativo",
        }
        assert 0 <= ejemplo["expected_relevancia_total"] <= 6