import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui.storage_demo import guardar_local


def click(app, label):
    next(b for b in app.button if b.label == label).click().run()
    assert not app.exception


def test_panel_arranca_sin_cargar_un_lote():
    """El punto de entrada debe cargar desde un clon limpio del repositorio.

    Detecta regresiones de integración, como importar el validador desde su
    antigua ubicación en la raíz, antes de probar acciones de la interfaz.
    No necesita credenciales de IA ni de OCI.
    """
    app = AppTest.from_file(str(ROOT / 'ui/app.py')).run()
    assert not app.exception, [error.message for error in app.exception]
    assert app.title[0].value == 'Empieza con tu comunidad'
    assert any(button.label == 'Probar con datos de ejemplo' for button in app.button)


def test_review_flow_and_edit_invalidation():
    app = AppTest.from_file(str(ROOT / 'ui/app.py')).run()
    click(app, 'Probar con datos de ejemplo')
    run_id = app.session_state.run_id
    click(app, 'Continuar al análisis')
    click(app, 'Analizar mensajes de demostración')
    summary = app.session_state.analisis.resumen_comunidad
    assert summary.total_interacciones_procesadas == 4
    assert summary.registros_validos == 3
    assert summary.registros_rechazados == 1
    click(app, 'Revisar borradores')
    click(app, 'Preparar borradores de demostración')
    click(app, 'Aprobar y continuar')
    assert app.session_state.revision.estado == 'pendiente'
    next(x for x in app.text_input if x.label == 'Nombre del revisor').set_value('Sergio').run()
    click(app, 'Aprobar y continuar')
    assert app.session_state.step == 3
    assert app.session_state.revision.fecha_decision is not None
    click(app, 'Volver a edición y aprobación')
    app.text_area(key='edit_li_body').set_value('Texto modificado que requiere una nueva revisión humana.').run()
    assert app.session_state.revision.estado == 'pendiente'
    assert app.session_state.revision.numero_revision == 2
    assert app.session_state.paquete is None
    click(app, '1  Carga')
    click(app, 'Probar con datos de ejemplo')
    assert app.session_state.run_id == run_id
    assert app.session_state.analisis is not None


def test_local_storage_idempotence_and_collision(tmp_path):
    package = {'activos': 'contenido aprobado'}
    path = guardar_local(package, 'activos/run/revision-001.json', tmp_path)
    assert json.loads(Path(path).read_text(encoding='utf-8')) == package
    assert guardar_local(package, 'activos/run/revision-001.json', tmp_path) == path
    with pytest.raises(ValueError):
        guardar_local({'activos': 'otro contenido'}, 'activos/run/revision-001.json', tmp_path)
    assert json.loads(Path(path).read_text(encoding='utf-8')) == package
