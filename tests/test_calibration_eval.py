"""Evaluación empírica de la calibración del catálogo few-shot
(tests/test_calibration_eval.py).

Verifica, contra un conjunto de validación reservado (*hold-out set*), que
el catálogo de ejemplos calibra al agente de análisis cognitivo:

1. Ejecuta el mismo hold-out en dos condiciones — Condición A (zero-shot,
   solo directivas + esquema) y Condición B (few-shot, directivas +
   catálogo) — y exige que B supere a A de forma estadísticamente
   significativa (prueba exacta de McNemar sobre pares discordantes).
2. Verifica, sobre la Condición B, la matriz completa de umbrales de
   aceptación del MVP (Kappa, F1 por clase, Macro F1, MAE, Spearman,
   trazabilidad e integridad de `motivo_seleccion`).

Requiere credenciales reales del proveedor LLM y un conjunto de validación
en `data/samples/validation_ground_truth.json` (ver informe de evaluación
de calibración, sección 1). Por ambos motivos se marca como prueba de
integración y se omite si faltan credenciales, siguiendo el mismo patrón
que `tests/test_oci_client.py`.

Dependencias de desarrollo adicionales requeridas (agregar a
`[dependency-groups] dev` en pyproject.toml): scikit-learn, scipy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from scipy.stats import binomtest, spearmanr
from sklearn.metrics import classification_report, cohen_kappa_score, mean_absolute_error

from ai_modules.multiprovider_agent import CognitiveAgentFactory

# CORRECCIÓN: la Condición B ya no se reconstruye concatenando
# INSTRUCCIONES_BASE + CATALOGO_FEW_SHOT a mano. Desde la corrección de
# analyzer.py, el prompt real de pre-producción es SYSTEM_PROMPT, que
# además del catálogo incluye el bloque de reanclaje de instrucciones
# (REANCLAJE_INSTRUCCIONES) después del catálogo. Si este harness seguía
# construyendo su propia versión abreviada, la prueba de calibración
# dejaba de medir el prompt que realmente corre en producción.
from ai_modules.prompts.analyzer import SYSTEM_PROMPT
from ai_modules.prompts.instructions import INSTRUCCIONES_BASE
from esquemas import AnalisisCognitivoPayload, InteraccionValidada

VALIDATION_PATH = Path("data/samples/validation_ground_truth.json")
PROVEEDOR_EVALUADO = "openai"  # motor titular; repetir con "anthropic"/"gemini" en Fase 4

UMBRALES = {
    "accuracy_sentimiento": 0.85,
    "macro_f1_sentimiento": 0.82,
    "kappa_enrutamiento": 0.80,
    "f1_por_clase_enrutamiento": 0.80,
    "mae_relevancia": 0.75,
    "alpha_significancia": 0.05,
}


def _credenciales_disponibles() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _credenciales_disponibles(),
        reason="requiere OPENAI_API_KEY para ejecutar la evaluación de calibración",
    ),
]


def _cargar_validacion() -> list[dict[str, Any]]:
    assert VALIDATION_PATH.exists(), (
        f"El conjunto de validación reservado no existe: {VALIDATION_PATH}. "
        "Ver informe de evaluación de calibración, sección 1."
    )
    with open(VALIDATION_PATH, encoding="utf-8") as f:
        datos = json.load(f)
    assert len(datos) >= 50, "El hold-out debe tener al menos 50 mensajes (sección 1 del informe)"
    return datos


def _ejecutar_lote(agent, interacciones: list[InteraccionValidada]) -> dict[str, Any]:
    """Ejecuta el agente sobre el lote completo y devuelve las predicciones
    indexadas por source_id.

    Deliberadamente NO se asume que el orden de salida del modelo coincide
    con el orden de entrada: un LLM procesando un lote no garantiza
    preservar la posición de cada elemento, así que emparejar por índice de
    lista arriesgaría comparar predicción y referencia de mensajes distintos
    sin que ningún error lo delate. El emparejamiento se hace por
    `source_id`, que es el campo de trazabilidad invariable del contrato.
    """
    resultado: AnalisisCognitivoPayload = agent.run_sync(interacciones).output
    por_id = {m.source_id: m for m in resultado.mensajes_analizados}
    esperados = {i.source_id for i in interacciones}
    faltantes = esperados - por_id.keys()
    assert not faltantes, (
        f"Trazabilidad incompleta: {len(faltantes)} source_id del lote no "
        f"aparecen en la respuesta del modelo: {sorted(faltantes)[:5]}"
    )
    return por_id


def _mcnemar_p_valor(acierto_a: list[bool], acierto_b: list[bool]) -> tuple[int, int, float]:
    """Prueba exacta de McNemar (unilateral) sobre pares discordantes.

    b = casos donde A acierta y B falla; c = casos donde A falla y B acierta.
    H0: P(b) == P(c) (ninguna condición es sistemáticamente mejor).
    H1: c > b (B, few-shot, mejora sobre A, zero-shot).
    """
    b = sum(a and not bb for a, bb in zip(acierto_a, acierto_b))
    c = sum((not a) and bb for a, bb in zip(acierto_a, acierto_b))
    if b + c == 0:
        return b, c, 1.0  # sin discrepancias entre condiciones: no hay evidencia de mejora
    p_valor = binomtest(c, b + c, p=0.5, alternative="greater").pvalue
    return b, c, p_valor


@pytest.fixture(scope="module")
def datos_validacion() -> list[dict[str, Any]]:
    return _cargar_validacion()


@pytest.fixture(scope="module")
def interacciones_entrada(datos_validacion) -> list[InteraccionValidada]:
    return [InteraccionValidada(**item["input"]) for item in datos_validacion]


def test_catalogo_supera_linea_base_y_umbrales_de_aceptacion(
    datos_validacion: list[dict[str, Any]],
    interacciones_entrada: list[InteraccionValidada],
) -> None:
    # CORRECCIÓN: validation_ground_truth.json representa cada entrada como
    # InteraccionValidada (campo `id`), no como MensajeAnalizado — `source_id`
    # no existe en el `input` de este archivo y solo aparece después, en la
    # salida del LLM. Leer aquí "source_id" sobre el input lanzaba KeyError
    # antes de llegar a invocar al agente. Las claves de referencia se
    # construyen con `id`; el emparejamiento contra las predicciones sigue
    # funcionando porque, por diseño, source_id (salida) debe copiar id
    # (entrada) sin cambios.
    ids = sorted(item["input"]["id"] for item in datos_validacion)
    y_true_sent = {d["input"]["id"]: d["expected_sentimiento"] for d in datos_validacion}
    y_true_ruta = {d["input"]["id"]: d["expected_enrutamiento"] for d in datos_validacion}
    y_true_rel = {d["input"]["id"]: d["expected_relevancia_total"] for d in datos_validacion}

    # --- Condición A: zero-shot (solo directivas + esquema, sin catálogo) ---
    agente_zero_shot = CognitiveAgentFactory.create_agent(
        PROVEEDOR_EVALUADO, INSTRUCCIONES_BASE, AnalisisCognitivoPayload
    )
    pred_a = _ejecutar_lote(agente_zero_shot, interacciones_entrada)

    # --- Condición B: few-shot (prompt completo de producción, catálogo +
    #     reanclaje de instrucciones) ---
    agente_few_shot = CognitiveAgentFactory.create_agent(
        PROVEEDOR_EVALUADO, SYSTEM_PROMPT, AnalisisCognitivoPayload
    )
    pred_b = _ejecutar_lote(agente_few_shot, interacciones_entrada)

    def alinear(pred: dict[str, Any], campo: str) -> list[Any]:
        return [getattr(pred[i], campo) for i in ids]

    y_true_sent_lista = [y_true_sent[i] for i in ids]
    y_true_ruta_lista = [y_true_ruta[i] for i in ids]
    y_true_rel_lista = [y_true_rel[i] for i in ids]

    y_pred_sent_a = alinear(pred_a, "sentimiento")
    y_pred_sent_b = alinear(pred_b, "sentimiento")
    y_pred_ruta_a = alinear(pred_a, "categoria_enrutamiento")
    y_pred_ruta_b = alinear(pred_b, "categoria_enrutamiento")
    y_pred_rel_b = [pred_b[i].puntuacion_relevancia.total for i in ids]

    # --- 1. Significancia estadística: Condición B debe superar a Condición A ---
    for nombre_tarea, y_true_lista, pred_a_lista, pred_b_lista in [
        ("sentimiento", y_true_sent_lista, y_pred_sent_a, y_pred_sent_b),
        ("enrutamiento", y_true_ruta_lista, y_pred_ruta_a, y_pred_ruta_b),
    ]:
        acierto_a = [p == t for p, t in zip(pred_a_lista, y_true_lista)]
        acierto_b = [p == t for p, t in zip(pred_b_lista, y_true_lista)]
        b, c, p_valor = _mcnemar_p_valor(acierto_a, acierto_b)
        assert p_valor < UMBRALES["alpha_significancia"], (
            f"[{nombre_tarea}] El catálogo (Condición B) no supera a zero-shot "
            f"(Condición A) de forma estadísticamente significativa "
            f"(McNemar exacto p={p_valor:.4f}, aciertos exclusivos de B={c}, de A={b})"
        )

    # --- 2. Matriz de umbrales de aceptación del MVP, sobre la Condición B ---
    reporte_sent = classification_report(
        y_true_sent_lista, y_pred_sent_b, output_dict=True, zero_division=0
    )
    reporte_ruta = classification_report(
        y_true_ruta_lista, y_pred_ruta_b, output_dict=True, zero_division=0
    )
    kappa_ruta = cohen_kappa_score(y_true_ruta_lista, y_pred_ruta_b)
    mae_rel = mean_absolute_error(y_true_rel_lista, y_pred_rel_b)
    rho_rel, _ = spearmanr(y_true_rel_lista, y_pred_rel_b)

    assert reporte_sent["accuracy"] >= UMBRALES["accuracy_sentimiento"], (
        f"Accuracy de sentimiento {reporte_sent['accuracy']:.2%} < "
        f"{UMBRALES['accuracy_sentimiento']:.0%}"
    )
    assert reporte_sent["macro avg"]["f1-score"] >= UMBRALES["macro_f1_sentimiento"], (
        f"Macro F1 de sentimiento {reporte_sent['macro avg']['f1-score']:.3f} < "
        f"{UMBRALES['macro_f1_sentimiento']}"
    )
    assert kappa_ruta >= UMBRALES["kappa_enrutamiento"], (
        f"Kappa de enrutamiento {kappa_ruta:.3f} < {UMBRALES['kappa_enrutamiento']}"
    )
    for clase, metricas in reporte_ruta.items():
        if clase in ("accuracy", "macro avg", "weighted avg"):
            continue
        assert metricas["f1-score"] >= UMBRALES["f1_por_clase_enrutamiento"], (
            f"F1 de enrutamiento para la clase '{clase}' = {metricas['f1-score']:.3f} "
            f"< {UMBRALES['f1_por_clase_enrutamiento']}"
        )
    assert mae_rel <= UMBRALES["mae_relevancia"], (
        f"MAE de relevancia {mae_rel:.3f} > {UMBRALES['mae_relevancia']}"
    )
    assert rho_rel > 0, (
        f"Correlación de Spearman no positiva en el orden de relevancia: {rho_rel:.3f}"
    )

    # --- 3. Invariantes estructurales de integridad (ya exigidas por Pydantic,
    #        verificadas aquí también como señal explícita de la prueba) ---
    for source_id, msg in pred_b.items():
        assert msg.source_id == source_id
        assert len(msg.motivo_seleccion) >= 10, f"motivo_seleccion corto para {source_id}"
