"""

Módulo de Validación Local de Andrea (Data Engineering).

Aplica la validación en dos capas:

1. Ingestión del sobre crudo con IngestionLote.

2. Validación registro por registro con InteraccionValidada.

3. Aislamiento de registros inválidos en auditoría sin tumbar el lote.

"""

import uuid

from datetime import datetime, timezone

from typing import Dict, List, Tuple

from esquemas import IngestionLote, InteraccionValidada, MetadataOrigenValidado



def adaptar_payload_oficial(payload_crudo: dict) -> dict:

    """

    Toma el JSON plano del jurado de la Hackatón y lo transforma en la

    estructura requerida por el esquema IngestionLote del equipo.

    """

    # 1. Inferir la plataforma de origen a partir del identificador de la comunidad

    origen = payload_crudo.get("origen_comunidad", "Desconocido")

    plataforma = origen.split("_")[0] if "_" in origen else origen



    # 2. Generar la estampa de tiempo en UTC bajo el estándar ISO-8601

    fecha_iso = datetime.now(timezone.utc).isoformat()



    # 3. Generar request_id único para trazabilidad del lote

    request_id = payload_crudo.get("request_id") or f"req_{uuid.uuid4().hex[:12]}"



    interacciones_adaptadas = []

    # 4. Iterar e inyectar IDs secuenciales a cada mensaje

    for posicion, item in enumerate(payload_crudo.get("interacciones", []), start=1):

        id_secuencial = f"msg_{posicion:03d}"  # Genera: msg_001, msg_002, etc.

        

        #Homologar la estructura inyectando el esquema de metadatos requerido

        sobre_individual = {

            "id": id_secuencial,

            "autor": item.get("autor", "AnÃ³nimo"),

            "canal": item.get("canal", "sin_canal"),

            "tipo": item.get("tipo", "sin_clasificar"),

            "texto": item.get("texto", ""),

            "metadata_origen": {

                "plataforma": plataforma,

                "identificador_original": f"autogenerado_{id_secuencial}",

                "fecha": fecha_iso

            }

        }

        # Agregar la interacciÃ³n estructurada a la lista de salida

        interacciones_adaptadas.append(sobre_individual)



    # 5. Estructurar sobre final homologado para IngestionLote

    payload_homologado = {

        "schema_version": "1.1.0",

        "request_id": request_id,

        "origen_comunidad": origen,

        "periodo_referencia": payload_crudo.get("periodo_referencia", "Sin_Periodo"),

        "interacciones": interacciones_adaptadas

    }



    return payload_homologado

    









def validar_lote_crudo(payload: IngestionLote, run_id: str | None = None) -> Tuple[List[InteraccionValidada], List[Dict]]:

    """

    Recibe un IngestionLote, valida cada interacciÃ³n y separa:

    - validos: List[InteraccionValidada] (pasan a los agentes de IA)

    - rechazados: List[Dict] (aislados en auditoría con su motivo de fallo)

    """

    validos: List[InteraccionValidada] = []

    rechazados: List[Dict] = []



    for raw_item in payload.interacciones:

        try:

            # Validar y parsear metadatos (fecha str -> datetime)

            meta_validada = MetadataOrigenValidado(

                plataforma=raw_item.metadata_origen.plataforma,

                identificador_original=raw_item.metadata_origen.identificador_original,

                fecha=raw_item.metadata_origen.fecha,

            )



            # Validar con el esquema estricto (exige texto no vacÃ­o)

            validated = InteraccionValidada(

                id=raw_item.id,

                autor=raw_item.autor,

                canal=raw_item.canal,

                tipo=raw_item.tipo,

                texto=raw_item.texto or "",

                metadata_origen=meta_validada,

            )

            validos.append(validated)

        except Exception as e:

            # Aislar el registro defectuoso en auditoría

            rechazados.append({

                "id": raw_item.id,

                "autor": raw_item.autor,

                "canal": raw_item.canal,

                "error": str(e),

                "texto_crudo": raw_item.texto,

                "metadata_origen": raw_item.metadata_origen.model_dump(),

            })



    return validos, rechazados
