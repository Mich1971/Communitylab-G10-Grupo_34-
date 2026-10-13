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
    def __init__(self, run_id, mensajes):
        self.run_id = run_id
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
