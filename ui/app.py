"""Panel HTTP de curaduría: Streamlit presenta; el backend decide y persiste."""

import copy
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import streamlit as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui.api_client import APIError, get_client

STEPS = ["Carga", "Resultados", "Edición y aprobación", "Almacenamiento"]


def init():
    defaults = {
        "step": 0,
        "current_run_id": st.query_params.get("run_id"),
        "active_revision_number": None,
        "loaded_package": None,
        "edit_run": None,
        "conflict": None,
        "uncertain": False,
        "poll_until": 0,
        "history_cursor": None,
        "message_cursor": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def run_path():
    return "/runs/" + quote(st.session_state.current_run_id, safe="")


def error(exc):
    st.error(str(exc))
    if exc.revision_actual is not None:
        st.warning(
            f"La revisión vigente es {exc.revision_actual}. Tu borrador no se ha reemplazado."
        )
    if exc.uncertain:
        st.warning(
            "La operación pudo haberse completado. Consulta las revisiones antes de intentar nuevamente."
        )


def read(api, suffix, **params):
    try:
        return api.get(run_path() + suffix, **params)
    except APIError as exc:
        error(exc)
        return None


def select_run(run_id):
    # Solo una acción explícita cambia el lote. El histórico no reemplaza
    # silenciosamente los campos de una edición que permanece en sesión.
    for key in list(st.session_state):
        if key.startswith("edit_"):
            del st.session_state[key]
    st.session_state.update(
        current_run_id=run_id,
        loaded_package=None,
        active_revision_number=None,
        conflict=None,
        uncertain=False,
        poll_until=time.monotonic() + 120,
        message_cursor=None,
    )
    st.query_params["run_id"] = run_id


def heading(title, text):
    st.title(title)
    st.caption(text)


def download(label, data, filename):
    st.download_button(
        label,
        json.dumps(data, ensure_ascii=False, indent=2),
        file_name=filename,
        mime="application/json",
    )


def show_detail(detail):
    if not detail:
        return
    st.caption("Procesamiento: " + detail["estado"])
    cols = st.columns(3)
    for col, key, label in zip(
        cols,
        ["total_recibidos", "total_validos", "total_rechazados"],
        ["Recibidos", "Válidos", "Rechazados"],
    ):
        col.metric(label, detail[key])
    if detail.get("advertencia"):
        st.warning(
            "El backend informa una inconsistencia entre el estado y el paquete. Requiere revisión técnica."
        )
    if detail["categoria_estado"] == "fallida":
        st.error(
            "El procesamiento falló. Los datos parciales disponibles no acreditan una ejecución exitosa."
        )
    elif detail["categoria_estado"] == "finalizada_sin_paquete":
        st.info(
            "La ejecución terminó sin paquete. Puedes consultar el análisis o los rechazos disponibles."
        )


def carga(api):
    heading(
        "Empieza con tu comunidad",
        "Envía un lote al backend o recupera una ejecución anterior.",
    )
    st.caption(
        "Cambiar de ejecución descarta la edición no guardada de la sesión. Descárgala antes de continuar."
    )
    uploaded = st.file_uploader("Selecciona un lote JSON", type=["json"])
    send = st.button("Enviar lote", disabled=uploaded is None, type="primary")
    example = st.button("Enviar datos de ejemplo")
    if send or example:
        try:
            raw = (
                uploaded.getvalue()
                if send
                else (ROOT / "data/raw/lote_prueba_01.json").read_bytes()
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("sobre inválido")
            if example:
                # Genera un request_id único en cada envío de ejemplo para que el
                # backend ejecute una corrida nueva en vez de devolver el duplicado previo.
                base_req = str(payload.get("request_id") or "req_ejemplo").strip()
                payload["request_id"] = f"{base_req}_{int(time.time())}"
            result = api.post("/interactions/process", payload)
            if not result.get("run_id"):
                raise APIError("La API no confirmó el identificador del lote.")
            select_run(result["run_id"])
            st.success(
                "Se recuperó la ejecución existente."
                if result.get("duplicado")
                else "Lote recibido por el backend."
            )
        except (ValueError, UnicodeError, TypeError):
            st.error("El archivo debe contener un objeto JSON válido.")
        except APIError as exc:
            error(exc)
    st.subheader("Histórico de ejecuciones")
    if st.button("Actualizar histórico"):
        st.session_state.history_cursor = None
    try:
        history = api.get("/runs", limite=20, cursor=st.session_state.history_cursor)
    except APIError as exc:
        error(exc)
        return
    items = history["items"]
    if items:
        choices = {x["run_id"]: x for x in items}
        chosen = st.selectbox(
            "Ejecución",
            list(choices),
            format_func=lambda rid: (
                f"{rid} · {choices[rid]['estado']} · {choices[rid].get('comunidad') or 'Comunidad pendiente'}"
            ),
        )
        if st.button("Abrir ejecución"):
            select_run(chosen)
            st.rerun()
    else:
        st.info("No hay ejecuciones en esta página.")
    if history.get("siguiente_cursor") and st.button("Siguiente página de ejecuciones"):
        st.session_state.history_cursor = history["siguiente_cursor"]
        st.rerun()
    if st.session_state.current_run_id:
        st.code(st.session_state.current_run_id, language=None)
        show_detail(read(api, "/detalle"))
        if st.button("Consultar resultados", type="primary"):
            st.session_state.step = 1
            st.rerun()


def sources(api, ids):
    ids = list(dict.fromkeys(ids))
    # La API admite hasta 50 fuentes por petición; no se truncan las citas.
    for start in range(0, len(ids), 50):
        data = read(api, "/fuentes", source_ids=ids[start : start + 50])
        if data is None:
            return
        for item in data["items"]:
            st.caption(
                f"{item['source_id']} · {item['autor']} · {item['canal']} · Original validado"
            )
            st.write(item["texto"])
        if data["no_encontrados"]:
            st.warning("Fuentes no encontradas: " + ", ".join(data["no_encontrados"]))


def resultados(api):
    heading(
        "Entiende a tu comunidad",
        "Resultados persistidos por el backend, con sus fuentes y motivos.",
    )
    detail = read(api, "/detalle")
    show_detail(detail)
    if not detail:
        return
    if detail["categoria_estado"] == "en_curso":
        st.info(
            "El lote sigue en cola o en procesamiento. El seguimiento automático dura hasta dos minutos."
        )
        if st.button("Reanudar seguimiento"):
            st.session_state.poll_until = time.monotonic() + 120
            st.rerun()

        @st.fragment(run_every=3)
        def seguimiento():
            if time.monotonic() >= st.session_state.poll_until:
                st.caption(
                    "Seguimiento pausado. Puedes reanudarlo sin reenviar el lote."
                )
                return
            state = read(api, "")
            if state:
                st.caption("Estado actual: " + state["estado"])
                if state["estado"] not in ("recibiendo", "en_cola", "procesando"):
                    st.session_state.poll_until = 0
                    st.rerun()

        seguimiento()
        return
    summary = read(api, "/resumen")
    if summary and summary.get("resumen"):
        data = summary["resumen"]
        st.write("Sentimiento predominante: " + data["sentimiento_predominante"])
        st.write("Temas: " + ", ".join(data["temas_principales"]))
        if data["alertas_soporte"]:
            st.write(data["alertas_soporte"])
    category = st.selectbox(
        "Categoría",
        ["Todas", "Logro", "Duda", "Dificultad"],
        key="category",
        on_change=lambda: st.session_state.update(message_cursor=None),
    )
    messages = read(
        api,
        "/mensajes",
        limite=50,
        cursor=st.session_state.message_cursor,
        categoria=None if category == "Todas" else category,
    )
    if messages:
        if not messages["disponible"]:
            st.info(
                "Análisis no disponible: " + (messages.get("motivo") or "sin datos")
            )
        elif not messages["items"]:
            st.info("No hay mensajes que coincidan con este filtro.")
        for item in messages["items"]:
            with st.expander(
                f"{item['source_id']} · {item['categoria_enrutamiento']} · {item['puntuacion']['total']}/6"
            ):
                st.write(item["motivo_seleccion"])
                st.caption("Sentimiento: " + item["sentimiento"])
                st.write("Temas: " + ", ".join(item["temas"]))
                st.json(item["puntuacion"], expanded=False)
                if item["requiere_soporte"]:
                    st.warning("Requiere soporte")
                st.caption(
                    "Apto para publicación: "
                    + ("Sí" if item["apto_para_publicacion"] else "No")
                )
        with st.expander("Fuentes de esta página"):
            sources(api, [m["source_id"] for m in messages["items"]])
        if messages.get("siguiente_cursor") and st.button(
            "Siguiente página de mensajes"
        ):
            st.session_state.message_cursor = messages["siguiente_cursor"]
            st.rerun()
        if st.session_state.message_cursor and st.button("Primera página de mensajes"):
            st.session_state.message_cursor = None
            st.rerun()
    rejects = read(api, "/rechazados")
    if rejects:
        with st.expander(f"Registros rechazados ({rejects['total']})"):
            for item in rejects["items"]:
                st.write(
                    f"{item.get('mensaje_id') or 'Sin identificador'} · {item['origen']}"
                )
                st.text(item["motivo"])


def load_revision(api, number):
    response = read(api, f"/revisiones/{number}")
    if response is None:
        return False
    package = response["paquete"]
    assets = package["activos"]
    st.session_state.update(
        loaded_package=package,
        active_revision_number=number,
        conflict=None,
        uncertain=False,
        edit_li_title=assets["post_linkedin"]["titulo"],
        edit_li_body=assets["post_linkedin"]["cuerpo"],
        edit_su_title=assets["resumen_semanal"]["titular"],
        edit_su_body=assets["resumen_semanal"]["resumen"],
    )
    return True


def edited_assets():
    assets = copy.deepcopy(st.session_state.loaded_package["activos"])
    assets["post_linkedin"].update(
        titulo=st.session_state.get("edit_li_title", assets["post_linkedin"]["titulo"]),
        cuerpo=st.session_state.get("edit_li_body", assets["post_linkedin"]["cuerpo"]),
    )
    assets["resumen_semanal"].update(
        titular=st.session_state.get(
            "edit_su_title", assets["resumen_semanal"]["titular"]
        ),
        resumen=st.session_state.get(
            "edit_su_body", assets["resumen_semanal"]["resumen"]
        ),
    )
    return assets


def edicion(api):
    heading(
        "Dale voz a tu comunidad",
        "Guarda una nueva revisión; las anteriores permanecen intactas.",
    )
    revisions = read(api, "/revisiones")
    if not revisions:
        return
    if not revisions["disponible"] or not revisions["items"]:
        st.info(
            "No hay revisión disponible: " + (revisions.get("motivo") or "sin paquete")
        )
        return
    if st.session_state.loaded_package is None and not load_revision(
        api, st.session_state.active_revision_number or revisions["revision_vigente"]
    ):
        return
    # Recargar requiere una decisión explícita; el polling nunca toca editores.
    numbers = [r["numero_revision"] for r in revisions["items"]]
    selected = st.selectbox("Revisión para consultar", numbers)
    st.caption(
        "Cargar una revisión reemplaza la edición de esta sesión. Puedes descargarla antes."
    )
    if st.button("Cargar revisión seleccionada"):
        load_revision(api, selected)
        st.rerun()
    if st.session_state.conflict or st.session_state.uncertain:
        st.warning(
            "Tu edición se conserva. Compara la revisión vigente antes de continuar; no se cambió la revisión base."
        )
        latest = read(api, f"/revisiones/{revisions['revision_vigente']}")
        if latest:
            with st.expander("Comparar con la revisión vigente"):
                st.json(latest["paquete"]["activos"])
    package = st.session_state.loaded_package
    st.caption(
        f"Revisión base {st.session_state.active_revision_number} · Estado guardado: {package['revision']['estado']}"
    )
    for tab, prefix in zip(
        st.tabs(["Post de LinkedIn", "Resumen semanal"]), ["li", "su"]
    ):
        with tab:
            editor, preview = st.columns(2)
            with editor:
                st.text_input("Título", key=f"edit_{prefix}_title")
                st.text_area("Contenido", key=f"edit_{prefix}_body", height=220)
            with preview:
                st.caption("Vista previa de la edición")
                st.subheader(st.session_state[f"edit_{prefix}_title"])
                st.write(st.session_state[f"edit_{prefix}_body"])
    assets = edited_assets()
    dirty = assets != package["activos"]
    if dirty:
        st.warning(
            "Hay cambios sin guardar. La aprobación de la revisión base no se aplica a estos textos."
        )
    with st.expander("Consultar mensajes fuente"):
        sources(
            api,
            assets["post_linkedin"]["source_ids"]
            + assets["resumen_semanal"]["source_ids"],
        )
    if assets.get("casos_no_publicables"):
        with st.expander("Casos no publicables"):
            st.json(assets["casos_no_publicables"])
    st.text_input("Nombre del revisor", key="reviewer")
    st.text_area("Comentarios de revisión", key="review_comments")
    st.caption(
        "Guardar, aprobar o rechazar crea una nueva revisión. Aprobar no publica ni confirma almacenamiento en OCI."
    )
    blocked = bool(st.session_state.conflict or st.session_state.uncertain)
    a, b, c = st.columns(3)
    decision = None
    if a.button("Guardar nueva revisión", disabled=blocked):
        decision = "pendiente"
    if b.button("Rechazar revisión", disabled=blocked):
        decision = "rechazado"
    if c.button("Aprobar revisión", type="primary", disabled=blocked):
        decision = "aprobado"
    download("Descargar edición sin guardar", assets, "borrador-sesion.json")
    if decision:
        reviewer = st.session_state.reviewer.strip()
        comments = st.session_state.review_comments.strip()
        if decision != "pendiente" and not reviewer:
            st.error("Escribe el nombre del revisor.")
            return
        if decision == "rechazado" and not comments:
            st.error("Indica el motivo del rechazo.")
            return
        try:
            result = api.post(
                run_path() + "/revisiones",
                {
                    "revision_base": st.session_state.active_revision_number,
                    "edicion": {
                        "estado": decision,
                        "revisor": reviewer or None,
                        "comentarios": comments or None,
                        "post_linkedin": assets["post_linkedin"],
                        "resumen_semanal": assets["resumen_semanal"],
                    },
                },
            )
            # Actualizar la base solo después de una confirmación explícita.
            # La recarga del paquete ocurre en el siguiente render, antes de
            # crear widgets, para no modificar claves de widgets ya dibujados.
            st.session_state.loaded_package = None
            st.session_state.active_revision_number = result["numero_revision"]
            st.rerun()
        except APIError as exc:
            st.session_state.conflict = (
                exc.revision_actual if exc.status == 409 else None
            )
            st.session_state.uncertain = exc.uncertain
            error(exc)


def almacenamiento(api):
    heading(
        "Conserva el contenido revisado",
        "Consulta la evidencia real de cada revisión y reintenta el guardado si hace falta.",
    )
    revisions = read(api, "/revisiones")
    if not revisions or not revisions["items"]:
        st.info("No hay paquete disponible para almacenar.")
        return
    number = st.selectbox(
        "Revisión almacenada", [r["numero_revision"] for r in revisions["items"]]
    )
    response = read(api, f"/revisiones/{number}")
    if not response:
        return
    package = response["paquete"]
    evidence = response["evidencia_oci"]
    st.caption("Revisión humana: " + package["revision"]["estado"])
    st.caption("OCI: " + evidence["status_almacenamiento"])
    if (
        st.session_state.loaded_package
        and edited_assets() != st.session_state.loaded_package["activos"]
    ):
        st.warning(
            "Hay textos sin guardar en el editor. Esta pantalla corresponde únicamente a la revisión persistida seleccionada."
        )
    ok = (
        evidence["status_almacenamiento"] == "guardado_con_exito"
        and evidence["comprobacion_lectura"] is True
    )
    if ok:
        st.success("Guardado en OCI y lectura verificada por el backend.")
        if st.session_state.get("oci_idempotent_revision") == number:
            st.info(
                "Reintento idempotente: el objeto ya existía en OCI con el mismo "
                "contenido y no se duplicó."
            )
    elif evidence["status_almacenamiento"] == "guardado_error":
        st.error(
            "El guardado OCI no está verificado. La revisión sigue disponible en el backend."
        )
    else:
        st.info("Esta revisión todavía no tiene almacenamiento OCI verificado.")
    st.code(evidence["ruta_objeto"], language=None, wrap_lines=True)
    st.caption("Guardar en OCI no significa publicar en LinkedIn.")
    if st.button("Guardar o reintentar en OCI", disabled=ok, type="primary"):
        try:
            result = api.post(run_path() + f"/revisiones/{number}/oci")
            if (
                result["status_almacenamiento"] == "guardado_con_exito"
                and result["comprobacion_lectura"] is True
            ):
                # El contrato persistido no guarda este indicador: solo viaja en
                # la respuesta del guardado, así que se recuerda para el rerun.
                st.session_state.oci_idempotent_revision = (
                    number if result.get("reintento_idempotente") else None
                )
                st.rerun()
            else:
                st.error(
                    "OCI no confirmó el guardado y la lectura. Puedes reintentar; no se declara éxito."
                )
        except APIError as exc:
            error(exc)
    download("Descargar revisión persistida", package, f"revision-{number:03d}.json")


def main():
    st.set_page_config(page_title="CommunityLab · Guía de curaduría", layout="wide")
    st.markdown(
        "<style>"
        + Path(__file__).with_name("styles.css").read_text(encoding="utf-8")
        + "</style>",
        unsafe_allow_html=True,
    )
    init()
    # Streamlit elimina claves de widgets que dejan de mostrarse. Reasignarlas
    # conserva el borrador al visitar Resultados o Almacenamiento.
    for key in list(st.session_state):
        if key.startswith("edit_") or key in ("reviewer", "review_comments"):
            st.session_state[key] = st.session_state[key]
    try:
        api = get_client()
    except ValidationError:
        st.title("CommunityLab · Configuración pendiente")
        st.error(
            "Configura COMMUNITYLAB_API_URL y COMMUNITYLAB_WEBHOOK_SECRET en el servidor antes de abrir el panel."
        )
        return
    with st.sidebar:
        st.image(
            str(Path(__file__).with_name("assets") / "communitylab-logo.png"), width=220
        )
        st.caption("Guía de curaduría · Backend HTTP")
        for index, step in enumerate(STEPS):
            if st.button(
                f"{index + 1}  {step}",
                disabled=index > 0 and not st.session_state.current_run_id,
                type="primary" if index == st.session_state.step else "secondary",
                use_container_width=True,
            ):
                st.session_state.step = index
                st.rerun()
        st.caption(
            "Procesamiento, revisión y almacenamiento son estados independientes."
        )
        st.caption("Las ediciones sin guardar solo permanecen durante esta sesión.")
    st.caption(
        f"Paso {st.session_state.step + 1} de 4 · {STEPS[st.session_state.step]}"
    )
    [carga, resultados, edicion, almacenamiento][st.session_state.step](api)


if __name__ == "__main__":
    main()
