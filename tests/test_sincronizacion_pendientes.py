import sqlite3

import pytest

from storage.local_db import DB_PATH, inicializar_db, sincronizar_pendientes_oci


# 1. Clase Mock para simular OCI sin tocar internet (Regla 3 y 4)
class MockUploadResult:
    def __init__(self, status_almacenamiento, mensaje_error=None):
        self.status_almacenamiento = status_almacenamiento
        self.mensaje_error = mensaje_error

class MockOCIClient:
    def __init__(self):
        self.llamadas = []

    def upload_package(self, package_payload, periodo_referencia, run_id, numero_revision, request_id):
        self.llamadas.append(run_id)
        # Simula éxito para el lote 1 y error persistente para el lote 2
        if run_id == "run-fallido-01":
            return MockUploadResult(status_almacenamiento="guardado_con_exito")
        return MockUploadResult(status_almacenamiento="guardado_error", mensaje_error="Red caída")

@pytest.fixture(autouse=True)
def setup_db_sincronizacion():
    """Fixture de aislamiento atómico para la base de datos."""
    inicializar_db()
    yield
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")
        cursor.execute("DELETE FROM paquetes_salida;")
        cursor.execute("DELETE FROM resumen_comunidad;")
        cursor.execute("DELETE FROM post_linkedin;")
        cursor.execute("DELETE FROM resumen_semanal;")
        cursor.execute("DELETE FROM evidencia_almacenamiento_oci;")
        cursor.execute("DELETE FROM lotes_procesamiento;")
        conn.commit()

def test_ciclo_sincronizacion_pendientes_parcial():
    """Certifica el comportamiento del motor ante un éxito y un fracaso continuo."""
    
    # 2. Inyección de 2 lotes con 'guardado_error' en SQLite
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")
        
        # Lote 1 (Se recuperará)
        cursor.execute("INSERT INTO lotes_procesamiento (run_id, request_id, origen_comunidad, periodo_referencia, total_recibidos, total_validos, total_rechazados, fecha_procesamiento) VALUES ('run-fallido-01', 'req-01', 'Slack', 'Semana_04', 1, 1, 0, '2026-10-17');")
        cursor.execute("INSERT INTO resumen_comunidad (run_id, total_interacciones_procesadas, registros_validos, registros_rechazados, sentimiento_predominante, fecha_generacion) VALUES ('run-fallido-01', 1, 1, 0, 'Positivo', '2026-10-17');")
        cursor.execute("INSERT INTO paquetes_salida (run_id, numero_revision, request_id, schema_version, fecha_generacion) VALUES ('run-fallido-01', 1, 'req-01', '1.1.0', '2026-10-17');")
        cursor.execute("INSERT INTO post_linkedin (run_id, numero_revision, titulo, cuerpo) VALUES ('run-fallido-01', 1, 'Título QA 1', 'Cuerpo largo válido para la prueba de LinkedIn.');")
        cursor.execute("INSERT INTO evidencia_almacenamiento_oci (run_id, numero_revision, bucket, ruta_objeto, status_almacenamiento, comprobacion_lectura) VALUES ('run-fallido-01', 1, 'bucket-qa', 'activos/01.json', 'guardado_error', 0);")
        
        # Lote 2 (Permanecerá en error)
        cursor.execute("INSERT INTO lotes_procesamiento (run_id, request_id, origen_comunidad, periodo_referencia, total_recibidos, total_validos, total_rechazados, fecha_procesamiento) VALUES ('run-fallido-02', 'req-02', 'Slack', 'Semana_04', 1, 1, 0, '2026-10-17');")
        cursor.execute("INSERT INTO resumen_comunidad (run_id, total_interacciones_procesadas, registros_validos, registros_rechazados, sentimiento_predominante, fecha_generacion) VALUES ('run-fallido-02', 1, 1, 0, 'Neutro', '2026-10-17');")
        cursor.execute("INSERT INTO paquetes_salida (run_id, numero_revision, request_id, schema_version, fecha_generacion) VALUES ('run-fallido-02', 1, 'req-02', '1.1.0', '2026-10-17');")
        cursor.execute("INSERT INTO post_linkedin (run_id, numero_revision, titulo, cuerpo) VALUES ('run-fallido-02', 1, 'Título QA 2', 'Cuerpo largo válido para la prueba de LinkedIn.');")
        cursor.execute("INSERT INTO evidencia_almacenamiento_oci (run_id, numero_revision, bucket, ruta_objeto, status_almacenamiento, comprobacion_lectura) VALUES ('run-fallido-02', 1, 'bucket-qa', 'activos/02.json', 'guardado_error', 0);")
        
        conn.commit()

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
        cursor = conn.get_cursor() if hasattr(conn, "get_cursor") else conn.cursor()
        
        cursor.execute("SELECT status_almacenamiento, comprobacion_lectura FROM evidencia_almacenamiento_oci WHERE run_id = 'run-fallido-01';")
        st1, rd1 = cursor.fetchone()
        assert st1 == "guardado_con_exito"
        assert rd1 == 1

        cursor.execute("SELECT status_almacenamiento, comprobacion_lectura FROM evidencia_almacenamiento_oci WHERE run_id = 'run-fallido-02';")
        st2, rd2 = cursor.fetchone()
        assert st2 == "guardado_error"
        assert rd2 == 0
