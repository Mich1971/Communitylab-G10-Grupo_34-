"""
Suite de Pruebas de Integración - Primera Prueba
Responsable: Deymar (Testing & Evidencias)

Verifica los criterios de aceptación fijados en la arquitectura:
1. Transporte y validación en 2 niveles (4 recibidos -> 3 válidos y 1 rechazado).
2. Aislamiento estricto de registros sin texto en auditoría.
3. Conformidad del paquete de salida con el contrato canónico PaqueteSalida.
"""

import os
import sys
from pathlib import Path

# Asegurar que el directorio raíz esté en sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json

from data_pipeline.validador import validar_lote_crudo
from esquemas import (
    ActivosGenerados,
    EvidenciaAlmacenamientoOCI,
    IngestionLote,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    ResumenComunidad,
    ResumenSemanal,
)


def test_validacion_lote_prueba_01():
    """
    Verifica que el lote sintético data/raw/lote_prueba_01.json
    resulte en exactamente 3 válidos y 1 rechazado (msg_004 sin texto).
    """
    ruta_archivo = Path("data/raw/lote_prueba_01.json")
    assert ruta_archivo.exists(), f"El archivo {ruta_archivo} debe existir"

    with open(ruta_archivo, "r", encoding="utf-8") as f:
        datos = json.load(f)

    # 1. Validación del sobre de transporte
    payload = IngestionLote(**datos)
    assert payload.request_id == "req-prueba-001"
    assert len(payload.interacciones) == 4

    # 2. Validación registro por registro (capa de Andrea)
    validos, rechazados = validar_lote_crudo(payload)

    # Conteos exactos esperados
    assert len(validos) == 3, f"Se esperaban 3 válidos, se obtuvieron {len(validos)}"
    assert len(rechazados) == 1, f"Se esperaba 1 rechazado, se obtuvieron {len(rechazados)}"

    # Verificar que el rechazado sea precisamente msg_004
    assert rechazados[0]["id"] == "msg_004"
    assert "string_too_short" in rechazados[0]["error"] or "vacío" in rechazados[0]["error"]

    # Verificar IDs de los válidos
    ids_validos = [v.id for v in validos]
    assert ids_validos == ["msg_001", "msg_002", "msg_003"]


def test_contrato_paquete_distribucion_generado():
    """
    Verifica que un paquete de salida simulado cumpla de forma estricta
    con el modelo PaqueteSalida (trazabilidad de source_ids y OCI).
    """
    paquete = PaqueteSalida(
        schema_version="1.1.0",
        run_id="run-2026-W38-a1b2c3d4",
        request_id="req-prueba-001",
        metadatos_ejecucion=MetadatosEjecucion(
            modelo="google-gemini-1.5-flash",
            prompt_version="v1.0",
            latencia_ms=1250,
            tokens_totales=950,
        ),
        analisis_resumido=ResumenComunidad(
            total_interacciones_procesadas=4,
            registros_validos=3,
            registros_rechazados=1,
            sentimiento_predominante="Altamente Positivo",
            distribucion_sentimiento={"positivo": 2, "neutro": 1},
            temas_principales=["Despliegue OCI", "Pydantic", "Webhook n8n"],
        ),
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(
                titulo="¡Éxito de la Comunidad: Despliegue en Oracle Cloud!",
                cuerpo="Nuestros miembros continúan alcanzando hitos impresionantes implementando soluciones en OCI.",
                hashtags=["#OracleCloud", "#ComunidadTech", "#CloudDev"],
                source_ids=["msg_001"],
            ),
            resumen_semanal=ResumenSemanal(
                seccion="Avances Técnicos y Soporte",
                titular="Resumen Semanal: Logros en la Nube y Soporte en Integraciones",
                resumen="Destacamos el avance con bases de datos en OCI y coordinamos apoyo técnico para flujos de n8n.",
                source_ids=["msg_001", "msg_002", "msg_003"],
            ),
        ),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            bucket="communitylab-activos-marketing",
            ruta_objeto="activos/2026-W38/run-2026-W38-a1b2c3d4/revision-001.json",
            status_almacenamiento="guardado_con_exito",
            comprobacion_lectura=True,
        ),
    )

    # Validar serialización JSON y propiedades
    data = paquete.model_dump(mode="json")
    assert data["run_id"] == "run-2026-W38-a1b2c3d4"
    assert data["almacenamiento_oci"]["comprobacion_lectura"] is True
    assert "msg_001" in data["activos"]["post_linkedin"]["source_ids"]
    assert len(data["activos"]["resumen_semanal"]["source_ids"]) == 3


if __name__ == "__main__":
    print("Ejecutando suite de pruebas de integración (Equipo 34)...")
    test_validacion_lote_prueba_01()
    print("  [OK] Criterio 1 - Transporte y Validación: 4 recibidos -> 3 válidos, 1 rechazado aislado (msg_004).")
    test_contrato_paquete_distribucion_generado()
    print("  [OK] Criterio 2 - Contrato PaqueteSalida: Tipado, trazabilidad source_ids y OCI verificados.")
    print("\n>>> RESULTADO: Todas las pruebas técnicas han sido superadas exitosamente.")
