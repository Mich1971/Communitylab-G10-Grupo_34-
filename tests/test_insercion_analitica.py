import sqlite3
from datetime import UTC, datetime

from esquemas import (
    AnalisisCognitivo,
    InteraccionValidada,
    MensajeAnalizado,
    MetadataOrigenValidado,
    PuntuacionRelevancia,
    ResumenComunidad,
)
from storage.local_db import DDL_ESQUELETO, registrar_analisis_bloque


# =============================================================================
# HELPER: Generador de datos reales de Pydantic para Pruebas Aisladas
# =============================================================================
def crear_interaccion_test(id_msg: str, texto: str) -> InteraccionValidada:
    return InteraccionValidada(
        id=id_msg,
        autor="Andrea Dev",
        canal="#general",
        tipo="testimonio",
        texto=texto,
        metadata_origen=MetadataOrigenValidado(
            plataforma="Discord",
            identificador_original=f"nativo_{id_msg}",
            fecha=datetime.now(UTC)
        )
    )

def crear_analisis_test(run_id: str, mensajes_analizados: list) -> AnalisisCognitivo:
    return AnalisisCognitivo(
        run_id=run_id,
        request_id=f"req_{run_id}",
        resumen_comunidad=ResumenComunidad(
            total_interacciones_procesadas=max(1, len(mensajes_analizados)),
            registros_validos=max(1, len(mensajes_analizados)),
            registros_rechazados=0,
            sentimiento_predominante="Altamente Positivo",
            distribucion_sentimiento={"Positivo": len(mensajes_analizados)},
            temas_principales=["SQLite"]
        ),
        mensajes_analizados=mensajes_analizados
    )

# =============================================================================
# SUITE DE PRUEBAS UNITARIAS DE INTEGRACIÓN LOCAL
# =============================================================================
def test_insercion_bloque_idempotencia_y_dlq(monkeypatch, tmp_path):
    """Valida la inserción correcta, aislamiento de DLQ e idempotencia con Pydantic real."""
    # 1. Setup: Configurar Base de Datos Temporal Aislada
    db_test_path = str(tmp_path / "test_communitylab.db")
    monkeypatch.setattr("storage.local_db.DB_PATH", db_test_path)
    
    with sqlite3.connect(db_test_path) as conn:
        conn.executescript(DDL_ESQUELETO)
    
    run_id = "run_integracion_01"
    
    # 2. Preparar Datos de Origen Reales (Pydantic)
    msg_sano = crear_interaccion_test("msg_001", "Código limpio en modo WAL")
    msg_malo = crear_interaccion_test("msg_002", "Texto defectuoso")
    interacciones_origen = [msg_sano, msg_malo]
    
    # 3. Preparar Respuestas de la IA Reales (Pydantic)
    ia_sana = MensajeAnalizado(
        source_id="msg_001",
        sentimiento="Altamente Positivo",
        categoria_enrutamiento="Logro",
        regla_aplicada="Regla de Producción",
        temas=["SQLite", "WAL"],
        entidades_relevantes=["Data"],
        puntuacion_relevancia=PuntuacionRelevancia(evidencia_explicita=2, utilidad_comunitaria=2, claridad_contexto=2),
        motivo_seleccion="Justificación válida de más de diez caracteres",
        requiere_soporte=False,
        apto_para_publicacion=True
    )
        # Usamos model_construct para evadir temporalmente la frontera de Pydantic 
    # y forzar un dato de prueba que viole la longitud en la base de datos (DLQ)
    ia_corrupta = MensajeAnalizado.model_construct(
        source_id="msg_002",
        sentimiento="Neutro",
        categoria_enrutamiento="Duda",
        regla_aplicada="Regla de Producción",
        temas=["Errores"],
        puntuacion_relevancia=PuntuacionRelevancia(evidencia_explicita=1, utilidad_comunitaria=0, claridad_contexto=0),
        motivo_seleccion="Corto",  # Evaluará tu regla de longitud en sqlite/DLQ
        requiere_soporte=True,
        apto_para_publicacion=False
    )
    
    analisis_ia = crear_analisis_test(run_id, [ia_sana, ia_corrupta])

    # 4. Ejecución de Intento 1 (Inserción Inicial)
    sanos, rechazados = registrar_analisis_bloque(analisis_ia, interacciones_origen)
    assert sanos == 1
    assert rechazados == 1

    # 5. Ejecución de Intento 2 (Reprocesamiento Idempotente del mismo run_id)
    # Volvemos a ejecutar la función. No debe arrojar errores de Clave Primaria.
    sanos_re, rechazados_re = registrar_analisis_bloque(analisis_ia, interacciones_origen)
    assert sanos_re == 1
    assert rechazados_re == 1

    # 6. Aseveraciones en Base de Datos (QA Data)
    with sqlite3.connect(db_test_path) as conn:
        cursor = conn.cursor()
        
        # Validar que la tabla madre tiene exactamente un registro por el run_id
        cursor.execute("SELECT COUNT(*) FROM lotes_procesamiento WHERE run_id=?;", (run_id,))
        assert cursor.fetchone()[0] == 1
        
        # Validar que solo el registro sano llegó a la tabla analítica nuclear
        cursor.execute("SELECT COUNT(*) FROM mensajes_analizados WHERE run_id=?;", (run_id,))
        assert cursor.fetchone()[0] == 1
        
        # Validar que las tablas relacionales N:M limpiaron el intento 1 y no duplicaron filas
        cursor.execute("SELECT COUNT(*) FROM mensaje_temas WHERE source_id='msg_001';")
        assert cursor.fetchone()[0] == 2  # Debe mantener el conteo original de temas ('SQLite' y 'WAL')
        
        # Validar el desvío quirúrgico al DLQ (auditoria_rechazados)
        cursor.execute("SELECT mensaje_id, error_detalle FROM auditoria_rechazados WHERE run_id=?;", (run_id,))
        fila_dlq = cursor.fetchone()
        assert fila_dlq is not None
        assert fila_dlq[0] == "msg_002"
        assert "ERROR_NEGOCIO_CHECK" in fila_dlq[1]
