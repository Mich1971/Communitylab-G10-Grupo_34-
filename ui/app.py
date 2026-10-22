"""Panel de curaduría. Ejecutar: python -m streamlit run ui/app.py."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from esquemas import ActivosGenerados, EstadoRevision, IngestionLote, MetadatosEjecucion, PaqueteSalida
# Consumir la validación oficial de Data Engineering desde su paquete actual.
# El módulo dejó de estar en la raíz; mantener una copia local duplicaría reglas.
from data_pipeline.validador import validar_lote_crudo
from ui.mock_pipeline import analizar_mock, generar_copys_mock, generar_run_id
from ui.storage_demo import guardar_local

STEPS = ['Carga', 'Resultados', 'Edición y aprobación', 'Almacenamiento']
st.set_page_config(page_title='CommunityLab · Guía de curaduría', layout='wide')
st.markdown('<style>' + Path(__file__).with_name('styles.css').read_text(encoding='utf-8') + '</style>', unsafe_allow_html=True)


def init():
    for key, value in dict(step=0, payload=None, validos=[], rechazados=[], analisis=None,
                           activos=None, revision=EstadoRevision(), evidencia=None,
                           paquete=None, fingerprint=None, run_id=None).items():
        st.session_state.setdefault(key, value)


def go(step):
    st.session_state.step = step


def heading(title, subtitle):
    st.title(title)
    st.markdown(f'<p class="subtitle">{escape(subtitle)}</p>', unsafe_allow_html=True)


def stats(items):
    for col, (number, label) in zip(st.columns(len(items)), items):
        col.markdown(f'<div class="stat"><strong>{number}</strong><span>{escape(label)}</span></div>', unsafe_allow_html=True)


def footer(previous=None, next_step=None, label='Continuar', disabled=False):
    st.divider()
    left, right = st.columns([2, 1])
    if previous is not None:
        left.button('Volver a ' + STEPS[previous].lower(), on_click=go, args=(previous,))
    if next_step is not None:
        right.button(label, type='primary', use_container_width=True,
                     on_click=go, args=(next_step,), disabled=disabled)


def load_batch(raw):
    fingerprint = hashlib.sha256(raw).hexdigest()
    if fingerprint == st.session_state.fingerprint:
        st.info('Este lote ya está cargado. Conservamos tu progreso.')
        return
    try:
        payload = IngestionLote.model_validate_json(raw)
        validos, rechazados = validar_lote_crudo(payload)
    except (ValidationError, ValueError) as exc:
        st.error('No pudimos cargar el lote. Revisa que sea un JSON con los campos requeridos.')
        if st.session_state.payload:
            st.info('El lote anterior y sus borradores siguen disponibles. Corrige el archivo y vuelve a validarlo.')
        with st.expander('Detalles para corregir el archivo'):
            st.text(str(exc))
        return
    for key in list(st.session_state):
        if key.startswith('edit_'):
            del st.session_state[key]
    st.session_state.update(payload=payload, validos=validos, rechazados=rechazados,
                            fingerprint=fingerprint, run_id=generar_run_id('lote'),
                            analisis=None, activos=None, evidencia=None, paquete=None,
                            revision=EstadoRevision())
    st.rerun()


def carga():
    heading('Empieza con tu comunidad', 'Carga tus conversaciones. Te ayudamos a convertirlas en contenido útil.')
    left, right = st.columns([1.6, 1], gap='large')
    with left:
        st.subheader('Importar mensajes')
        st.caption('Archivos JSON · La validación conservará los registros que puedan procesarse.')
        uploaded = st.file_uploader('Selecciona un lote JSON', type=['json'])
        if st.session_state.payload:
            st.caption('Un lote diferente reemplazará los borradores de esta sesión. Descárgalos antes de continuar.')
        if st.button('Validar archivo', type='primary', disabled=uploaded is None):
            load_batch(uploaded.getvalue())
        st.divider()
        st.markdown('**¿Quieres explorar primero?**')
        st.caption('El ejemplo incluye 4 mensajes sintéticos: 3 válidos y 1 vacío.')
        if st.button('Probar con datos de ejemplo'):
            load_batch((ROOT / 'data/raw/lote_prueba_01.json').read_bytes())
    with right:
        st.subheader('Resumen de validación')
        if not st.session_state.payload:
            st.info('Tu resumen aparecerá aquí cuando cargues un lote.')
        else:
            stats([(len(st.session_state.payload.interacciones), 'Recibidos'),
                   (len(st.session_state.validos), 'Válidos'), (len(st.session_state.rechazados), 'Rechazados')])
            st.caption(st.session_state.payload.origen_comunidad + ' · ' + st.session_state.payload.periodo_referencia)
            if st.session_state.validos:
                st.success(f'{len(st.session_state.validos)} mensajes listos. Puedes continuar aunque haya registros rechazados.')
            for item in st.session_state.rechazados:
                with st.expander('Registro rechazado · ' + item['id']):
                    st.warning('El mensaje está vacío.' if not (item.get('texto_crudo') or '').strip()
                               else 'El registro contiene datos inválidos. Revisa sus campos y la fecha.')
                    st.caption('Los demás mensajes válidos pueden continuar.')
                    # El diagnóstico original se conserva para soporte sin presentarlo
                    # como la instrucción principal para quien revisa el lote.
                    st.caption('Detalle técnico de validación')
                    st.code(item['error'], language=None, wrap_lines=True)
    if st.session_state.validos:
        st.subheader('Vista previa de mensajes')
        for item in st.session_state.validos:
            st.caption(f'{item.autor} · {item.canal} · {item.id}')
            st.write(item.texto)
        footer(next_step=1, label='Continuar al análisis')
    elif st.session_state.payload:
        st.warning('No hay mensajes válidos para continuar. Corrige el archivo y vuelve a cargarlo.')


def resultados():
    heading('Entiende a tu comunidad', 'Revisa los mensajes y descubre qué convertir en contenido.')
    if not st.session_state.validos:
        st.info('Carga un lote con mensajes válidos para empezar.')
        footer(previous=0)
        return
    if st.session_state.analisis is None:
        st.info(f'{len(st.session_state.validos)} mensajes listos para analizar. Esta demostración usa reglas locales, no una IA real.')
        if st.button('Analizar mensajes de demostración', type='primary'):
            with st.spinner('Analizando mensajes…'):
                result = analizar_mock(st.session_state.validos, st.session_state.run_id, st.session_state.payload.request_id)
                result.resumen_comunidad.registros_rechazados = len(st.session_state.rechazados)
                result.resumen_comunidad.total_interacciones_procesadas = len(st.session_state.payload.interacciones)
                st.session_state.analisis = result
            st.rerun()
        footer(previous=0)
        return
    messages = st.session_state.analisis.mensajes_analizados
    st.info('Análisis de demostración terminado. Revisa las fuentes antes de preparar los borradores; las categorías se obtuvieron con reglas locales.')
    stats([(len(messages), 'mensajes analizados'),
           (sum(m.categoria_enrutamiento == 'Logro' for m in messages), 'logros'),
           (sum(m.categoria_enrutamiento == 'Duda' for m in messages), 'dudas')])
    st.divider()
    title, filter_col = st.columns([3, 1])
    title.subheader('Análisis por mensaje')
    title.caption('Consulta las fuentes antes de revisar los borradores.')
    category = filter_col.selectbox('Filtrar por categoría', ['Todas las categorías', 'Logro', 'Duda', 'Dificultad'])
    sources = {m.id: m for m in st.session_state.validos}
    filtered = [m for m in messages if category == 'Todas las categorías' or m.categoria_enrutamiento == category]
    if not filtered:
        st.info('No hay mensajes en esta categoría.')
    for msg in filtered:
        a, b, c = st.columns([1, 2.1, 1.8], gap='large')
        a.markdown('**' + escape(msg.source_id) + '**')
        color = {'Logro': 'achievement', 'Duda': 'question', 'Dificultad': 'difficulty'}[msg.categoria_enrutamiento]
        a.markdown(f'<span class="badge {color}">{msg.categoria_enrutamiento}</span>', unsafe_allow_html=True)
        a.caption(f'Relevancia {msg.puntuacion_relevancia.total}/6')
        source = sources.get(msg.source_id)
        b.caption(f'{source.autor} · {source.canal}' if source else 'Fuente no disponible')
        b.write(source.texto if source else msg.source_id)
        c.caption('POR QUÉ ES RELEVANTE')
        c.write(msg.motivo_seleccion)
        c.caption('Sentimiento: ' + msg.sentimiento)
        if msg.requiere_soporte:
            c.warning('Necesita apoyo')
        st.divider()
    footer(previous=0, next_step=2, label='Revisar borradores')


def invalidate():
    old = st.session_state.revision
    if old.estado != 'pendiente' or st.session_state.evidencia:
        st.session_state.revision = EstadoRevision(numero_revision=old.numero_revision + 1)
    st.session_state.evidencia = None
    st.session_state.paquete = None


def edicion():
    heading('Dale voz a tu comunidad', 'Edita los borradores, comprueba sus fuentes y registra tu decisión.')
    if st.session_state.analisis is None:
        st.info('Completa el análisis antes de preparar contenido.')
        footer(previous=1)
        return
    if st.session_state.activos is None:
        st.info('Prepararemos dos borradores de ejemplo: una publicación para LinkedIn y un resumen semanal.')
        if st.button('Preparar borradores de demostración', type='primary'):
            st.session_state.activos = generar_copys_mock(st.session_state.analisis)
            if st.session_state.activos is None:
                st.warning('No hay mensajes aptos para publicación. Prueba con otro lote que incluya logros.')
            else:
                st.rerun()
        footer(previous=1)
        return
    assets = st.session_state.activos
    fields = [('li_title', assets.post_linkedin.titulo), ('li_body', assets.post_linkedin.cuerpo),
              ('su_title', assets.resumen_semanal.titular), ('su_body', assets.resumen_semanal.resumen)]
    for key, value in fields:
        st.session_state.setdefault('edit_' + key, value)
    for tab, prefix, ids in zip(st.tabs(['Post de LinkedIn', 'Resumen semanal']), ['li', 'su'],
                                [assets.post_linkedin.source_ids, assets.resumen_semanal.source_ids]):
        with tab:
            editor, preview = st.columns([1.2, 1], gap='large')
            with editor:
                st.text_input('Título', key='edit_' + prefix + '_title', on_change=invalidate)
                st.text_area('Contenido', key='edit_' + prefix + '_body', height=200, on_change=invalidate)
            with preview:
                st.caption('VISTA PREVIA')
                st.subheader(st.session_state['edit_' + prefix + '_title'])
                st.write(st.session_state['edit_' + prefix + '_body'])
            with st.expander('Consultar mensajes fuente'):
                for source in st.session_state.validos:
                    if source.id in ids:
                        st.caption(f'{source.id} · {source.autor} · {source.canal}')
                        st.write(source.texto)
    st.divider()
    revision = st.session_state.revision
    st.subheader(f'Revisión {revision.numero_revision} · {revision.estado.capitalize()}')
    # La decisión humana es independiente de publicar o persistir el paquete.
    if revision.estado == 'rechazado':
        st.warning('Borradores rechazados. Ajusta el contenido y registra una nueva decisión para continuar.')
    elif revision.estado == 'aprobado':
        st.success('Revisión aprobada. Puedes continuar al almacenamiento desde el menú. Editar el contenido requerirá otra aprobación.')
    else:
        st.info('Pendiente de revisión: comprueba ambos formatos y sus fuentes antes de aprobar.')
    st.caption('Cambiar un texto aprobado exige una nueva revisión. Guardar un borrador no lo aprueba.')
    st.caption('Aprobar no publica en LinkedIn ni sube archivos a OCI. El nombre del revisor es obligatorio; para rechazar, agrega un motivo.')
    reviewer = st.text_input('Nombre del revisor')
    comments = st.text_area('Comentarios de revisión', height=80)
    buttons = st.columns(3)
    save = buttons[0].button('Guardar borrador en sesión')
    reject = buttons[1].button('Rechazar borradores')
    approve = buttons[2].button('Aprobar y continuar', type='primary', use_container_width=True)
    if save or reject or approve:
        data = assets.model_dump()
        data['post_linkedin'].update(titulo=st.session_state.edit_li_title, cuerpo=st.session_state.edit_li_body)
        data['resumen_semanal'].update(titular=st.session_state.edit_su_title, resumen=st.session_state.edit_su_body)
        try:
            validated = ActivosGenerados.model_validate(data)
        except ValidationError:
            st.error('Cada título necesita al menos 5 caracteres y cada contenido, 20. Revisa ambos formatos.')
            return
        if (approve or reject) and not reviewer.strip():
            st.error('Escribe el nombre del revisor para registrar la decisión.')
            return
        if reject and not comments.strip():
            st.error('Indica el motivo del rechazo en los comentarios.')
            return
        st.session_state.activos = validated
        if save:
            st.success('Borrador conservado en esta sesión. Descárgalo para guardarlo fuera del navegador.')
        else:
            invalidate()
            st.session_state.revision = EstadoRevision(numero_revision=st.session_state.revision.numero_revision,
                estado='aprobado' if approve else 'rechazado', revisor=reviewer.strip(),
                comentarios=comments.strip(), fecha_decision=datetime.now(timezone.utc))
            if approve:
                go(3)
            st.rerun()
    draft = st.session_state.activos.model_dump()
    draft['post_linkedin'].update(titulo=st.session_state.edit_li_title, cuerpo=st.session_state.edit_li_body)
    draft['resumen_semanal'].update(titular=st.session_state.edit_su_title, resumen=st.session_state.edit_su_body)
    st.download_button('Descargar borrador JSON', json.dumps(draft, ensure_ascii=False, indent=2),
                       file_name='borrador.json', mime='application/json')
    footer(previous=1)


def almacenamiento():
    heading('Conserva el contenido revisado', 'Guarda el paquete aprobado y comprueba que puede recuperarse.')
    st.info('Modo demostración: el archivo se guarda en este equipo. No se ha conectado OCI.')
    if st.session_state.revision.estado != 'aprobado':
        st.warning('Revisa y aprueba los borradores antes de guardarlos.')
        footer(previous=2)
        return
    revision = st.session_state.revision
    stats([(revision.numero_revision, 'revisión aprobada'), (2, 'formatos de contenido')])
    st.caption('Revisado por ' + revision.revisor)
    if st.session_state.paquete is None:
        st.session_state.paquete = PaqueteSalida(run_id=st.session_state.run_id,
            request_id=st.session_state.payload.request_id,
            metadatos_ejecucion=MetadatosEjecucion(modelo='mock-local', prompt_version='v0-frontend', latencia_ms=0, tokens_totales=0),
            analisis_resumido=st.session_state.analisis.resumen_comunidad,
            activos=st.session_state.activos, revision=revision,
            almacenamiento_oci={'ruta_objeto': f'activos/{st.session_state.run_id}/revision-{revision.numero_revision:03d}.json'})
    package = st.session_state.paquete
    if not st.session_state.evidencia:
        st.info('Paquete aprobado, pendiente de guardar. Guarda y verifica la copia local o descarga el paquete.')
    if st.button('Guardar y verificar copia local', type='primary'):
        try:
            st.session_state.evidencia = guardar_local(package.model_dump(mode='json'), package.almacenamiento_oci.ruta_objeto)
        except (OSError, ValueError):
            st.error('No pudimos guardar o verificar la copia. El borrador sigue disponible; puedes descargarlo o reintentar.')
    if st.session_state.evidencia:
        st.success('Copia local guardada y verificada')
        st.caption('Se recuperó el archivo y su contenido coincide. Esta comprobación no acredita almacenamiento en OCI.')
        st.code(st.session_state.evidencia, language=None, wrap_lines=True)
        st.caption('Puedes descargar el paquete aprobado o volver a edición. Una modificación requerirá una nueva revisión y otro guardado.')
    st.download_button('Descargar paquete aprobado', package.model_dump_json(indent=2),
                       file_name=f'revision-{revision.numero_revision:03d}.json', mime='application/json')
    with st.expander('Detalles técnicos del paquete'):
        st.json(package.model_dump(mode='json'))
    footer(previous=2)


def main():
    init()
    with st.sidebar:
        st.image(str(Path(__file__).with_name('assets') / 'communitylab-logo.png'), width=220)
        st.caption('Guía de curaduría')
        st.markdown('<div class="nav-space"></div>', unsafe_allow_html=True)
        available = [True, bool(st.session_state.validos), st.session_state.analisis is not None,
                     st.session_state.revision.estado == 'aprobado']
        descriptions = ['Importar conversaciones', 'Entender los mensajes', 'Revisar y ajustar', 'Conservar una copia']
        for index, step in enumerate(STEPS):
            st.button(f'{index + 1}  {step}', key=f'nav_{index}', use_container_width=True,
                type='primary' if index == st.session_state.step else 'secondary',
                disabled=not available[index], on_click=go, args=(index,))
            st.caption(descriptions[index])
        st.divider()
        st.caption('Modo demostración\n\nAnálisis y generación simulados. Almacenamiento local.')
        st.caption('El progreso se conserva durante esta sesión.')
    st.markdown('<div class="mode">Demostración · análisis y generación simulados · almacenamiento local · OCI no conectado</div>', unsafe_allow_html=True)
    # Es un indicador de navegación, no un porcentaje de ejecución del backend.
    st.caption(f'Paso {st.session_state.step + 1} de {len(STEPS)} · {STEPS[st.session_state.step]}')
    [carga, resultados, edicion, almacenamiento][st.session_state.step]()


if __name__ == '__main__':
    main()
