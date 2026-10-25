import sqlite3
from datetime import UTC, datetime

import pytest

from esquemas import (
    ActivosGenerados,
    AnalisisCognitivo,
    EstadoRevision,
    EvidenciaAlmacenamientoOCI,
    InteraccionValidada,
    MensajeAnalizado,
    MetadataOrigenValidado,
    MetadatosEjecucion,
    PaqueteSalida,
    PostLinkedIn,
    PuntuacionRelevancia,
    ResumenComunidad,
    ResumenSemanal,
)
from storage.local_db import (
    DB_PATH,
    inicializar_db,
    persistir_resultado,
    sincronizar_pendientes_oci,
)


# 1. Clase Mock para simular OCI sin tocar internet (Regla 3 y 4)
class MockUploadResult:
    def __init__(self, status_almacenamiento, comprobacion_lectura=True, mensaje_error=None):
        self.status_almacenamiento = status_almacenamiento
        self.comprobacion_lectura = comprobacion_lectura
        self.mensaje_error = mensaje_error


class MockOCIClient:
    def __init__(self):
        self.llamadas = []

    def upload_package(self, package_payload, periodo_referencia, run_id, numero_revision, request_id):
        self.llamadas.append(run_id)
        # Simula éxito para el lote 1 y error persistente para el lote 2
        if run_id == "run-fallido-01":
            return MockUploadResult(status_almacenamiento="guardado_con_exito", comprobacion_lectura=True)
        return MockUploadResult(status_almacenamiento="guardado_error", comprobacion_lectura=False, mensaje_error="Red caída")


@pytest.fixture(autouse=True)
def setup_db_sincronizacion():
    """Fixture de aislamiento atómico para la base de datos."""
    inicializar_db()
    yield
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")
        cursor.execute("DELETE FROM paquetes_salida;")
        cursor.execute("DELETE FROM metadatos_ejecucion_paquete;")
        cursor.execute("DELETE FROM resumen_comunidad;")
        cursor.execute("DELETE FROM mensajes_analizados;")
        cursor.execute("DELETE FROM post_linkedin;")
        cursor.execute("DELETE FROM resumen_semanal;")
        cursor.execute("DELETE FROM evidencia_almacenamiento_oci;")
        cursor.execute("DELETE FROM lotes_procesamiento;")
        conn.commit()


def _sembrar_lote_fallido(run_id: str, request_id: str, titulo: str) -> None:
    resumen = ResumenComunidad(
        total_interacciones_procesadas=1,
        registros_validos=1,
        registros_rechazados=0,
        sentimiento_predominante="Positivo",
        distribucion_sentimiento={"Positivo": 1},
        temas_principales=["Slack"],
        alertas_soporte=[],
    )
    paquete = PaqueteSalida(
        run_id=run_id,
        request_id=request_id,
        schema_version="1.1.0",
        metadatos_ejecucion=MetadatosEjecucion(
            modelo="gpt-4o-mini",
            prompt_version="1.0.0",
            latencia_ms=100,
            tokens_totales=200,
        ),
        analisis_resumido=resumen,
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(
                titulo=titulo,
                canal_recomendado="LinkedIn Oficial",
                cuerpo="Cuerpo largo válido para la prueba de LinkedIn.",
                hashtags=["#Comunidad"],
                source_ids=["msg_001"],
            ),
            resumen_semanal=ResumenSemanal(
                seccion="Actividad",
                titular="Resumen semanal de prueba",
                resumen="Resumen de actividad semanal suficientemente extenso.",
                source_ids=["msg_001"],
            ),
            casos_no_publicables=[],
        ),
        revision=EstadoRevision(numero_revision=1, estado="pendiente"),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            bucket="bucket-qa",
            ruta_objeto=f"activos/{run_id}.json",
            status_almacenamiento="guardado_error",
            comprobacion_lectura=False,
        ),
    )
    analisis = AnalisisCognitivo(
        run_id=run_id,
        request_id=request_id,
        resumen_comunidad=resumen,
        mensajes_analizados=[
            MensajeAnalizado(
                source_id="msg_001",
                sentimiento="Positivo",
                categoria_enrutamiento="Logro",
                regla_aplicada="Regla de prueba",
                temas=["Slack"],
                puntuacion_relevancia=PuntuacionRelevancia(
                    evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2
                ),
                motivo_seleccion="Motivo con más de diez caracteres.",
                apto_para_publicacion=True,
            )
        ],
    )
    interacciones = [
        InteraccionValidada(
            id="msg_001",
            autor="Usuario",
            canal="#slack",
            tipo="mensaje",
            texto="Texto de prueba para el lote.",
            metadata_origen=MetadataOrigenValidado(
                plataforma="Slack",
                identificador_original="slack_001",
                fecha=datetime.now(UTC),
            ),
        )
    ]
    persistir_resultado(
        run_id=run_id,
        request_id=request_id,
        origen_comunidad="Slack",
        periodo_referencia="Semana_04",
        total_recibidos=1,
        total_validos=1,
        analisis_cognitivo=analisis,
        interacciones_origen=interacciones,
        paquete=paquete,
    )


def test_ciclo_sincronizacion_pendientes_parcial():
    """Certifica el comportamiento del motor ante un éxito y un fracaso continuo."""

    # 2. Inyección de 2 lotes con 'guardado_error' en SQLite
    _sembrar_lote_fallido("run-fallido-01", "req-01", "Título QA 1")
    _sembrar_lote_fallido("run-fallido-02", "req-02", "Título QA 2")

    mock_client = MockOCIClient()

    # 3. Ejecución del motor asíncrono
    exitosos, fallidos = sincronizar_pendientes_oci(mock_client)

    # 4. Aserciones de control (Regla 4)
    assert exitosos == 1
    assert fallidos == 1
    assert "run-fallido-01" in mock_client.llamadas
    assert "run-fallido-02" in mock_client.llamadas

    # 5. Verificar persistencia e integridad final en la DB
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT status_almacenamiento, comprobacion_lectura FROM evidencia_almacenamiento_oci WHERE run_id = 'run-fallido-01';")
        st1, rd1 = cursor.fetchone()
        assert st1 == "guardado_con_exito"
        assert rd1 == 1

        cursor.execute("SELECT status_almacenamiento, comprobacion_lectura FROM evidencia_almacenamiento_oci WHERE run_id = 'run-fallido-02';")
        st2, rd2 = cursor.fetchone()
        assert st2 == "guardado_error"
        assert rd2 == 0
