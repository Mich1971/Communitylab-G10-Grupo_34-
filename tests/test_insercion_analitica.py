import pytest
import sqlite3
from storage.local_db import registrar_analisis_bloque, DDL_ESQUELETO

# =============================================================================
# MOCKS ESTRUCTURADOS CON COMPORTAMIENTO DE CONTRATO (esquemas.py)
# =============================================================================
class MockPuntuacion:
    def __init__(self, e, u, c):
        self.evidencia_explicita = e
        self.utilidad_comunitaria = u
        self.claridad_contexto = c
        self.total = e + u + c

class MockMensajeIA:
    def __init__(self, source_id, sentimiento, cat, temas, entidades, requiere_soporte, apto):
        self.source_id = source_id
        self.sentimiento = sentimiento
        self.categoria_enrutamiento = cat
        self.regla_aplicada = "Regla de prueba automatizada"
        self.temas = temas
        self.entidades_relevantes = entidades
        self.puntuacion_relevancia = MockPuntuacion(2, 2, 2)
        self.motivo_seleccion = "Justificación válida de más de diez caracteres"
        self.requiere_soporte = requiere_soporte
        self.apto_para_publicacion = apto

class MockAnalisisCognitivo:
    def __init__(self, run_id, mensajes, request_id="req_mock_default"):
        self.run_id = run_id
        self.request_id = request_id  # Soluciona el fallo de compatibilidad con el DLQ
        self.mensajes_analizados = mensajes


class MockInteraccionValidada:
    def __init__(self, id_msg, autor, canal, texto):
        self.id = id_msg
        self.autor = autor
        self.canal = canal
        self.tipo = "testimonio"
        self.texto = texto

# =============================================================================
# SUITE DE PRUEBA UNITARIA Y DE INTEGRACIÓN
# =============================================================================
def test_insercion_bloque_resiliencia_y_tipos(monkeypatch, tmp_path):
    """Valida la inserción correcta, la conversión de tipos y la tolerancia a IDs huérfanos."""
    
    # 1. Setup: Base de datos temporal aislada para la prueba
    db_test_path = str(tmp_path / "test_communitylab.db")
    monkeypatch.setattr("storage.local_db.DB_PATH", db_test_path)
    
    conn = sqlite3.connect(db_test_path)
    conn.executescript(DDL_ESQUELETO)
    
    # Insertar el lote padre mandatorio para cumplir la FK del DDL
    conn.execute("""
        INSERT INTO lotes_procesamiento VALUES 
        ('run_01', 'req_01', 'Discord', 'Semana_04', 4, 3, 1, '2026-10-13');
    """)
    conn.commit()

    # 2. Preparar el lote mixto (Datos buenos + Datos con errores de integridad)
    msg_valido = MockInteraccionValidada("msg_001", "Andrea Dev", "#general", "Código limpio en WAL")
    msg_limite = MockInteraccionValidada("msg_002", "Sergio UI", "#alerts", "Frontera crítica")
    interacciones_origen = [msg_valido, msg_limite]

    # Mensajes de la IA incluyendo el caso huérfano (msg_999 no existe en el origen)
    ia_bueno = MockMensajeIA("msg_001", "Altamente Positivo", "Logro", ["SQLite", "WAL"], ["Data"], False, True)
    ia_limite = MockMensajeIA("msg_002", "Dificultad/Negativo", "Dificultad", ["Errores"], [], True, False)
    ia_huerfano = MockMensajeIA("msg_999", "Neutro", "Duda", ["Test"], [], False, False)
    
    analis_ia = MockAnalisisCognitivo("run_01", [ia_bueno, ia_limite, ia_huerfano])

    # 3. Ejecución del pipeline bajo prueba
    registrar_analisis_bloque(analis_ia, interacciones_origen)

    # 4. Aseveraciones (Asserts) de Calidad de Datos
    cursor = conn.cursor()
    
    # Validar que se ignoró el registro huérfano de forma segura y se guardaron solo 2
    cursor.execute("SELECT COUNT(*) FROM mensajes_analizados;")
    assert cursor.fetchone()[0] == 2

    # Validar la conversión estricta de booleanos a enteros (0/1) en la base de datos
    cursor.execute("SELECT requiere_soporte, apto_para_publicacion FROM mensajes_analizados WHERE source_id='msg_001';")
    req_soporte, apto_pub = cursor.fetchone()
    assert req_soporte == 0
    assert apto_pub == 1

    cursor.execute("SELECT requiere_soporte, apto_para_publicacion FROM mensajes_analizados WHERE source_id='msg_002';")
    req_soporte_2, apto_pub_2 = cursor.fetchone()
    assert req_soporte_2 == 1
    assert apto_pub_2 == 0

    # Validar la integridad relacional en cascada de los temas asociados
    cursor.execute("SELECT tema FROM mensaje_temas WHERE source_id='msg_001';")
    temas = [row[0] for row in cursor.fetchall()]
    assert "SQLite" in temas
    assert "WAL" in temas

    conn.close()

def test_idempotencia_reprocesamiento_bloque(monkeypatch, tmp_path):
    """Garantiza que el reprocesamiento del mismo lote no duplique metadatos N:M (Regla 4)."""
    
    # 1. Setup: Base de datos efímera aislada
    db_test_path = str(tmp_path / "test_idempotencia.db")
    monkeypatch.setattr("storage.local_db.DB_PATH", db_test_path)
    
    conn = sqlite3.connect(db_test_path)
    conn.executescript(DDL_ESQUELETO)
    
    # Insertar registro padre mandatorio para cumplir FK
    conn.execute("""
        INSERT INTO lotes_procesamiento VALUES 
        ('run_idemp_01', 'req_idemp_01', 'Slack', 'Semana_04', 1, 1, 0, '2026-10-16');
    """)
    conn.commit()

    # 2. Preparar datos de prueba (1 mensaje con 2 temas y 1 entidad)
    msg_origen = MockInteraccionValidada("msg_idemp_01", "Andrea Core", "#dev", "Idempotencia probada en Sandbox")
    interacciones_origen = [msg_origen]

    ia_analisis = MockMensajeIA(
        "msg_idemp_01", 
        "Altamente Positivo", 
        "Logro", 
        ["Idempotencia", "SQLite"],  # 2 Temas
        ["Oracle"],                   # 1 Entidad
        False, 
        True
    )
    analis_ia = MockAnalisisCognitivo("run_idemp_01", [ia_analisis])

    # 3. Ejecución del pipeline: Intento 1
    registrar_analisis_bloque(analis_ia, interacciones_origen)

    # 4. Ejecución del pipeline: Intento 2 (Reprocesamiento directo del mismo lote)
    registrar_analisis_bloque(analis_ia, interacciones_origen)

    # 5. Aseveraciones de Calidad de Datos (QA Data)
    cursor = conn.cursor()
    
    # La tabla nuclear debe tener un único registro (INSERT OR REPLACE)
    cursor.execute("SELECT COUNT(*) FROM mensajes_analizados WHERE run_id='run_idemp_01';")
    assert cursor.fetchone()[0] == 1

    # REGLA 4: Las tablas N:M NO deben duplicar filas. Deben mantener el conteo original.
    cursor.execute("SELECT COUNT(*) FROM mensaje_temas WHERE source_id='msg_idemp_01';")
    assert cursor.fetchone()[0] == 2  # Si fallara la purga, contaría 4

    cursor.execute("SELECT COUNT(*) FROM mensaje_entidades WHERE source_id='msg_idemp_01';")
    assert cursor.fetchone()[0] == 1  # Si fallara la purga, contaría 2

    conn.close()
    
def test_dead_letter_queue_aislamiento(monkeypatch, tmp_path):
    """Garantiza que los registros corruptos sean desviados por la función DLQ (Regla 4)."""
    
    # 1. Setup: Base de datos aislada
    db_test_path = str(tmp_path / "test_dlq.db")
    monkeypatch.setattr("storage.local_db.DB_PATH", db_test_path)
    
    conn = sqlite3.connect(db_test_path)
    conn.executescript(DDL_ESQUELETO)
    
    # Registro padre mandatorio
    conn.execute("""
        INSERT INTO lotes_procesamiento VALUES 
        ('run_dlq_01', 'req_dlq_01', 'Discord', 'Semana_04', 1, 0, 1, '2026-10-17');
    """)
    conn.commit()

    # 2. Mock con error deliberado (motivo_seleccion menor a 10 caracteres)
    msg_origen = MockInteraccionValidada("msg_corrupto_01", "Sergio UI", "#alerts", "Mensaje defectuoso")
    interacciones_origen = [msg_origen]

    ia_analisis = MockMensajeIA(
        "msg_corrupto_01", "Neutro", "Duda", ["Test"], [], False, False
    )
    # Forzar el fallo de longitud de forma quirúrgica
    ia_analisis.motivo_seleccion = "Corto" 
    
    analis_ia = MockAnalisisCognitivo("run_dlq_01", [ia_analisis])
    # Inyectar el request_id requerido por el contrato del DLQ
    analis_ia.request_id = "req_dlq_01"

    # 3. Ejecución
    registrar_analisis_bloque(analis_ia, interacciones_origen)

    # 4. Asserts: Validación de aislamiento en el DLQ
    cursor = conn.cursor()
    
    # No debe haberse guardado en la tabla nuclear
    cursor.execute("SELECT COUNT(*) FROM mensajes_analizados WHERE source_id='msg_corrupto_01';")
    assert cursor.fetchone()[0] == 0

    # Debe estar registrado detalladamente en la auditoría del DLQ
    cursor.execute("""
        SELECT mensaje_id, error_detalle, texto_crudo 
        FROM auditoria_rechazados 
        WHERE run_id='run_dlq_01';
    """)
    fila_dlq = cursor.fetchone()
    
    assert fila_dlq is not None
    assert fila_dlq[0] == "msg_corrupto_01"
    assert "ERROR_DDL_CHECK" in fila_dlq[1]
    assert fila_dlq[2] == "Mensaje defectuoso"

    conn.close()

def test_dlq_lote_mixto_metricas(monkeypatch, tmp_path):
    """Valida la bifurcación de un lote mixto y el retorno correcto de los conteos exactos."""
    
    # 1. Setup: Base de datos aislada
    db_test_path = str(tmp_path / "test_mixto.db")
    monkeypatch.setattr("storage.local_db.DB_PATH", db_test_path)
    
    conn = sqlite3.connect(db_test_path)
    conn.executescript(DDL_ESQUELETO)
    
    # Registro padre mandatorio
    conn.execute("""
        INSERT INTO lotes_procesamiento VALUES 
        ('run_mixto_01', 'req_mixto_01', 'Discord', 'Semana_04', 3, 2, 1, '2026-10-17');
    """)
    conn.commit()

    # 2. Mocks de Origen (3 interacciones válidas en ingesta)
    msg_sano_1 = MockInteraccionValidada("msg_ok_01", "Andrea Core", "#dev", "Texto válido uno")
    msg_sano_2 = MockInteraccionValidada("msg_ok_02", "Sergio UI", "#design", "Texto válido dos")
    msg_malo = MockInteraccionValidada("msg_bad_01", "Miembro Anon", "#help", "Texto que fallará")
    interacciones_origen = [msg_sano_1, msg_sano_2, msg_malo]

    # Mocks de IA (2 correctos + 1 con motivo corto)
    ia_ok_1 = MockMensajeIA("msg_ok_01", "Positivo", "Logro", ["IA"], [], False, True)
    ia_ok_2 = MockMensajeIA("msg_ok_02", "Neutro", "Duda", ["UI"], [], False, False)
    ia_bad = MockMensajeIA("msg_bad_01", "Dificultad/Negativo", "Dificultad", ["Bug"], [], True, False)
    ia_bad.motivo_seleccion = "Corto" # Provoca desvío al DLQ

    analis_ia = MockAnalisisCognitivo("run_mixto_01", [ia_ok_1, ia_ok_2, ia_bad], request_id="req_mixto_01")

    # 3. Ejecución y Captura de Métricas Retornadas (Optimización)
    sanos, rechazados = registrar_analisis_bloque(analis_ia, interacciones_origen)

    # 4. Aserciones de las Métricas de Retorno
    assert sanos == 2
    assert rechazados == 1

    # 5. Aserciones de Persistencia en Base de Datos
    cursor = conn.cursor()
    
    # Verificar almacenamiento analítico sano
    cursor.execute("SELECT COUNT(*) FROM mensajes_analizados WHERE run_id='run_mixto_01';")
    assert cursor.fetchone()[0] == 2

    # Verificar aislamiento en el DLQ
    cursor.execute("SELECT mensaje_id, error_detalle FROM auditoria_rechazados WHERE run_id='run_mixto_01';")
    fila_dlq = cursor.fetchone()
    assert fila_dlq is not None
    assert fila_dlq[0] == "msg_bad_01"
    assert "ERROR_DDL_CHECK" in fila_dlq[1]

    conn.close()
