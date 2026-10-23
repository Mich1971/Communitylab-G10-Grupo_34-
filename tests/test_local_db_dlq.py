import sqlite3

from storage.local_db import DB_PATH, inicializar_db, registrar_rechazados_validador


def test_registrar_rechazados_validador_guarda_en_dlq():
    """Verifica que los descartes se guarden en el DLQ respetando las llaves foráneas."""
    inicializar_db()

    run_id_test = "run_test_local_db_001"
    request_id_test = "req_test_12345"

    # 1. SETUP: Insertar el lote maestro para cumplir la restricción de integridad (FK)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO lotes_procesamiento (
            run_id, request_id, origen_comunidad, periodo_referencia, 
            total_recibidos, total_validos, total_rechazados, fecha_procesamiento
        ) VALUES (?, ?, 'Discord_Test', 'Semana_04', 3, 2, 1, '2026-10-17T00:00:00Z');
        """,
        (run_id_test, request_id_test)
    )
    conn.commit()
    conn.close()

    # 2. ENTRADA DEL VALIDADOR
    error_simulado = [{
        "run_id": run_id_test,
        "request_id": request_id_test,
        "mensaje_id": "msg_003_corrupto",
        "autor": "Usuario Invalido",
        "canal": "#soporte",
        "error": "El texto de la interacción está vacío o contiene solo espacios.",
        "texto_crudo": "   ",
        "fecha_aislamiento": "2026-10-17T00:00:00Z"
    }]

    # 3. EJECUCIÓN
    conteo_insertados = registrar_rechazados_validador(error_simulado)
    assert conteo_insertados == 1

    # 4. VERIFICACIÓN FÍSICA
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT mensaje_id, error_detalle FROM auditoria_rechazados WHERE run_id = ?;",
        (run_id_test,)
    )
    registro = cursor.fetchone()
    conn.close()

    assert registro is not None
    assert registro[0] == "msg_003_corrupto"
    assert "solo espacios" in registro[1]
