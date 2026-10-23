import pytest
from esquemas import IngestionLote
from validador import adaptar_payload_oficial, validar_lote_crudo


def test_adaptador_y_validacion_payload_oficial():
    # 1. ENTRADA: JSON plano e idéntico al formato oficial del jurado
    payload_jurado_oficial = {
        "origen_comunidad": "Discord_Grupo_ONE_G10",
        "periodo_referencia": "Semana_04",
        "interacciones": [
            {
                "autor": "Mariana Souza",
                "canal": "#logros-y-empleos",
                "tipo": "testimonio",
                "texto": "Comunidad, quede seleccionada para el puesto de Desarrolladora Junior de IA! El proyecto del curso de LangChain y OCI que construi en mi portfolio marco toda la diferencia en la entrevista tecnica.",
            },
            {
                "autor": "Lucas Albuquerque",
                "canal": "#dudas-langgraph",
                "tipo": "pregunta_tecnica",
                "texto": "Tengo dudas sobre como estructurar los nodos condicionales en LangGraph cuando la respuesta del LLM necesita reintento.",
            },
            {
                "autor": "Usuario Invalido",
                "canal": "#soporte",
                "tipo": "pregunta_tecnica",
                "texto": "   ",  # Mensaje corrupto (solo espacios) para validar la tubería de auditoría
            },
        ],
    }

    # 2. EJECUCIÓN: Pasar los datos planos por tu adaptador de frontera
    payload_adaptado = adaptar_payload_oficial(payload_jurado_oficial)

    # 3. VERIFICACIONES: Validar las inyecciones de datos
    assert "schema_version" in payload_adaptado
    assert payload_adaptado["schema_version"] == "1.1.0"
    assert "request_id" in payload_adaptado
    assert payload_adaptado["request_id"].startswith("req_")

    for idx, msg in enumerate(payload_adaptado["interacciones"], start=1):
        assert "id" in msg
        assert msg["id"] == f"msg_{idx:03d}"
        assert "metadata_origen" in msg
        assert msg["metadata_origen"]["plataforma"] == "Discord"
        assert msg["metadata_origen"]["identificador_original"] == f"autogenerado_{msg['id']}"
        assert "fecha" in msg["metadata_origen"]

    # 4. CAPA DE ADMISIÓN: Instanciar el objeto Pydantic oficial del equipo
    lote_pydantic = IngestionLote(**payload_adaptado)

    # 5. SEGREGACIÓN: Clasificar registros válidos y aislar corruptos
    validos, rechazados = validar_lote_crudo(lote_pydantic)

    # 6. ASERCIONES: Confirmar la división de los datos
    assert len(validos) == 2
    assert validos[0].autor == "Mariana Souza"
    assert validos[1].autor == "Lucas Albuquerque"

    assert len(rechazados) == 1
    assert rechazados[0]["id"] == "msg_003"
    assert "El texto de la interacción no puede estar vacío" in rechazados[0]["error"]
