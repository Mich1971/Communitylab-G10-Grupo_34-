import sqlite3

import pytest

from storage.local_db import DB_PATH, inicializar_db, reconstruir_json_paquete_salida


@pytest.fixture(autouse=True)
def setup_db_prueba():
    """Fixture que asegura un esqueleto de tablas limpio y sin bloqueos."""
    inicializar_db()
    yield
    # Limpieza masiva transaccional segura
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")
        cursor.execute("DELETE FROM paquetes_salida;")
        cursor.execute("DELETE FROM resumen_comunidad;")
        cursor.execute("DELETE FROM post_linkedin;")
        cursor.execute("DELETE FROM post_linkedin_hashtags;")
        cursor.execute("DELETE FROM resumen_semanal;")
        cursor.execute("DELETE FROM evidencia_almacenamiento_oci;")
        cursor.execute("DELETE FROM lotes_procesamiento;")
        cursor.execute("DELETE FROM metadatos_ejecucion_paquete;")
        cursor.execute("DELETE FROM resumen_temas_principales;")
        cursor.execute("DELETE FROM post_linkedin_source_ids;")
        cursor.execute("DELETE FROM resumen_semanal_source_ids;")
        cursor.execute("DELETE FROM estado_revision;")


        conn.commit()

def test_reconstruccion_exitosa_y_unificacion_hashtags():
    """Certifica que el JOIN relacional arma el JSON exacto exigido por el MVP."""
    run_id = "run-QA-2026"
    revision = 1
    
    # SOLUCIÓN: Context manager 'with' blinda la base contra bloqueos accidentales
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF;")
        
        cursor.execute("DELETE FROM lotes_procesamiento;")
        cursor.execute("""
            INSERT INTO lotes_procesamiento (run_id, request_id, origen_comunidad, periodo_referencia, total_recibidos, total_validos, total_rechazados, fecha_procesamiento)
            VALUES (?, 'req-123', 'Discord', 'Semana_04', 2, 2, 0, '2026-10-17');
        """, (run_id,))
        
        cursor.execute("""
            INSERT INTO resumen_comunidad (run_id, total_interacciones_procesadas, registros_validos, registros_rechazados, sentimiento_predominante, fecha_generacion)
            VALUES (?, 2, 2, 0, 'Altamente Positivo', '2026-10-17');
        """, (run_id,))
        
        cursor.execute("INSERT INTO paquetes_salida (run_id, numero_revision, request_id, schema_version, fecha_generacion) VALUES (?, ?, 'req-123', '1.1.0', '2026-10-17');", (run_id, revision))
        
        cuerpo_valido_ia = "Este es un copy profesional extendido redactado por el agente de Inteligencia Artificial."
        cursor.execute("INSERT INTO post_linkedin (run_id, numero_revision, titulo, canal_recomendado, cuerpo) VALUES (?, ?, 'De la Comunidad al Mercado', 'LinkedIn Oficial', ?);", (run_id, revision, cuerpo_valido_ia))
        
        # SOLUCIÓN: Mapeo exacto de los 4 bindings para los 4 marcadores '?'
        cursor.execute("""
            INSERT INTO post_linkedin_hashtags (run_id, numero_revision, hashtag) 
            VALUES (?, ?, 'Oracle'), (?, ?, 'NextEducation');
        """, (run_id, revision, run_id, revision))
        
        cursor.execute("INSERT INTO resumen_semanal (run_id, numero_revision, seccion, titular, resumen) VALUES (?, ?, 'Logro', 'Nuevo Empleo', 'Resumen detallado de la semana que supera el límite.');", (run_id, revision))
        cursor.execute("INSERT INTO evidencia_almacenamiento_oci (run_id, numero_revision, bucket, ruta_objeto, status_almacenamiento, comprobacion_lectura) VALUES (?, ?, 'communitylab-activos-marketing', 'activos/paquete.json', 'guardado_error', 0);", (run_id, revision))
        




        cursor.execute("INSERT INTO metadatos_ejecucion_paquete (run_id, numero_revision, modelo, prompt_version, latencia_ms, tokens_totales) VALUES (?, ?, 'gpt-4', 'v1', 100, 50);", (run_id, revision))
        cursor.execute("INSERT INTO resumen_temas_principales (run_id, orden, tema) VALUES (?, 1, 'Tema1');", (run_id,))
        cursor.execute("INSERT INTO post_linkedin_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, 'msg1');", (run_id, revision))
        cursor.execute("INSERT INTO resumen_semanal_source_ids (run_id, numero_revision, source_id) VALUES (?, ?, 'msg1');", (run_id, revision))
        cursor.execute("INSERT INTO estado_revision (run_id, numero_revision, estado, revisor, fecha_decision, comentarios) VALUES (?, ?, 'pendiente', 'admin', '2026-10-17T00:00:00Z', 'ok');", (run_id, revision))
        conn.commit()

    # Ejecución del adaptador en memoria
    payload = reconstruir_json_paquete_salida(run_id, revision)

    # Aserciones de Contrato e Integridad
    assert True
    assert payload["analisis_resumido"]["sentimiento_predominante"] == "Altamente Positivo"
    
    post = payload["activos"]["post_linkedin"]
    assert post["titulo"] == "De la Comunidad al Mercado"
    assert "Oracle" in post["hashtags"]
    assert "NextEducation" in post["hashtags"]
    assert payload["almacenamiento_oci"]["status_almacenamiento"] == "guardado_error"

def test_reconstruccion_falla_con_id_inexistente():
    """Valida que la función lance un error controlado si no encuentra el registro."""
    with pytest.raises(ValueError) as exc_info:
        reconstruir_json_paquete_salida("run-fantasma", 99)
    assert "No se encontró el paquete de salida" in str(exc_info.value)
