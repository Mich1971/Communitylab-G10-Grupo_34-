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
    reconstruir_json_paquete_salida,
)


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
        cursor.execute("DELETE FROM metadatos_ejecucion_paquete;")
        cursor.execute("DELETE FROM resumen_comunidad;")
        cursor.execute("DELETE FROM mensajes_analizados;")
        cursor.execute("DELETE FROM post_linkedin;")
        cursor.execute("DELETE FROM post_linkedin_hashtags;")
        cursor.execute("DELETE FROM resumen_semanal;")
        cursor.execute("DELETE FROM evidencia_almacenamiento_oci;")
        cursor.execute("DELETE FROM lotes_procesamiento;")
        conn.commit()


def test_reconstruccion_exitosa_y_unificacion_hashtags():
    """Certifica que el JOIN relacional arma el JSON exacto exigido por el MVP."""
    run_id = "run-QA-2026"
    revision = 1

    resumen = ResumenComunidad(
        total_interacciones_procesadas=1,
        registros_validos=1,
        registros_rechazados=0,
        sentimiento_predominante="Altamente Positivo",
        distribucion_sentimiento={"Positivo": 1},
        temas_principales=["Oracle", "NextEducation"],
        alertas_soporte=[],
    )

    paquete = PaqueteSalida(
        run_id=run_id,
        request_id="req-123",
        schema_version="1.1.0",
        metadatos_ejecucion=MetadatosEjecucion(
            modelo="gpt-4o-mini",
            prompt_version="1.0.0",
            latencia_ms=120,
            tokens_totales=250,
        ),
        analisis_resumido=resumen,
        activos=ActivosGenerados(
            post_linkedin=PostLinkedIn(
                titulo="De la Comunidad al Mercado",
                canal_recomendado="LinkedIn Oficial",
                cuerpo="Este es un copy profesional extendido redactado por el agente de Inteligencia Artificial.",
                hashtags=["#Oracle", "#NextEducation"],
                source_ids=["msg_001"],
            ),
            resumen_semanal=ResumenSemanal(
                seccion="Logro",
                titular="Nuevo Empleo",
                resumen="Resumen detallado de la semana que supera el límite establecido.",
                source_ids=["msg_001"],
            ),
            casos_no_publicables=[],
        ),
        revision=EstadoRevision(
            numero_revision=revision,
            estado="pendiente",
        ),
        almacenamiento_oci=EvidenciaAlmacenamientoOCI(
            bucket="communitylab-activos-marketing",
            ruta_objeto="activos/paquete.json",
            status_almacenamiento="guardado_error",
            comprobacion_lectura=False,
        ),
    )

    analisis = AnalisisCognitivo(
        run_id=run_id,
        request_id="req-123",
        resumen_comunidad=resumen,
        mensajes_analizados=[
            MensajeAnalizado(
                source_id="msg_001",
                sentimiento="Positivo",
                categoria_enrutamiento="Logro",
                regla_aplicada="Regla de prueba",
                temas=["Oracle"],
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
            autor="Mariana",
            canal="#general",
            tipo="testimonio",
            texto="Texto de prueba",
            metadata_origen=MetadataOrigenValidado(
                plataforma="Discord",
                identificador_original="orig_1",
                fecha=datetime.now(UTC),
            ),
        )
    ]

    persistir_resultado(
        run_id=run_id,
        request_id="req-123",
        origen_comunidad="Discord",
        periodo_referencia="Semana_04",
        total_recibidos=1,
        total_validos=1,
        analisis_cognitivo=analisis,
        interacciones_origen=interacciones,
        paquete=paquete,
    )

    # Ejecución del adaptador en memoria
    payload = reconstruir_json_paquete_salida(run_id, revision)

    # Aserciones de Contrato e Integridad
    assert payload["schema_version"] == "1.1.0"
    assert payload["analisis_resumido"]["sentimiento_predominante"] == "Altamente Positivo"

    post = payload["activos"]["post_linkedin"]
    assert post["titulo"] == "De la Comunidad al Mercado"
    assert post["hashtags"] == ["#Oracle", "#NextEducation"]
    assert payload["almacenamiento_oci"]["status_almacenamiento"] == "guardado_error"


def test_reconstruccion_falla_con_id_inexistente():
    """Valida que la función lance un error controlado si no encuentra el registro."""
    with pytest.raises(ValueError) as exc_info:
        reconstruir_json_paquete_salida("run-fantasma", 99)
    assert "No se encontró el paquete de salida" in str(exc_info.value)
