from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest
from test_consultas import _sembrar

from api.config import Settings
from api.main import create_app
from api.registro import RegistroIdempotencia
from storage.local_db import LocalDatabaseManager
from ui.api_client import APIClient, UISettings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def panel(monkeypatch, tmp_path):
    secret = "secreto-frontend-prueba-123"
    cfg = Settings(webhook_secret=secret, data_dir=tmp_path, _env_file=None)
    LocalDatabaseManager(str(cfg.db_path))
    RegistroIdempotencia(cfg.db_path).inicializar()
    _sembrar(str(cfg.db_path), "run-ui-test")
    tc = TestClient(create_app(cfg))

    def transport(request):
        response = tc.request(
            request.method,
            str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=dict(response.headers),
        )

    client = APIClient(
        UISettings(api_url="http://testserver", webhook_secret=secret, _env_file=None),
        transport=httpx.MockTransport(transport),
    )
    monkeypatch.setattr("ui.api_client.get_client", lambda: client)
    app = AppTest.from_file(str(ROOT / "ui/app.py"), default_timeout=20).run()
    return app, client


def click(app, label):
    next(b for b in app.button if b.label == label).click().run()
    assert not app.exception


def test_read_edit_approve_and_conflict(panel):
    app, client = panel
    assert not app.exception
    click(app, "Abrir ejecución")
    click(app, "3  Edición y aprobación")
    app.text_input(key="reviewer").set_value("Sergio").run()
    click(app, "Aprobar revisión")
    assert app.session_state.active_revision_number == 2
    assert (
        client.get("/runs/run-ui-test/revisiones/2")["paquete"]["revision"]["estado"]
        == "aprobado"
    )
    app.text_area(key="edit_li_body").set_value(
        "Una edición humana que debe conservarse ante conflictos concurrentes."
    ).run()
    client.post(
        "/runs/run-ui-test/revisiones",
        {"revision_base": 2, "edicion": {"estado": "pendiente"}},
    )
    click(app, "Guardar nueva revisión")
    assert app.session_state.active_revision_number == 2
    assert "conservarse" in app.session_state.edit_li_body
    assert app.warning or app.error


def test_history_results_sources_and_stored_evidence(panel):
    app, _ = panel
    click(app, "Abrir ejecución")
    click(app, "2  Resultados")
    assert not app.exception
    click(app, "4  Almacenamiento")
    assert not app.exception
    assert any("OCI" in c.value for c in app.caption) or app.info


def test_unsaved_text_survives_navigation(panel):
    app, _ = panel
    click(app, "Abrir ejecución")
    click(app, "3  Edición y aprobación")
    text = "Este texto aún no se ha guardado y debe sobrevivir la navegación."
    app.text_area(key="edit_li_body").set_value(text).run()
    click(app, "2  Resultados")
    click(app, "4  Almacenamiento")
    assert any("sin guardar" in w.value for w in app.warning)
    click(app, "3  Edición y aprobación")
    assert app.text_area(key="edit_li_body").value == text


def test_oci_200_error_is_not_success(panel, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "storage.revisiones.OCIStorageClient",
        lambda: SimpleNamespace(
            upload_package=lambda **kw: SimpleNamespace(
                status_almacenamiento="guardado_error",
                comprobacion_lectura=False,
                mensaje_error="Fallo controlado",
                bucket="test",
                reintento_idempotente=False,
            )
        ),
    )
    app, client = panel
    click(app, "Abrir ejecución")
    click(app, "4  Almacenamiento")
    click(app, "Guardar o reintentar en OCI")
    assert not app.success
    assert app.error
    assert (
        client.get("/runs/run-ui-test/revisiones/1")["evidencia_oci"][
            "comprobacion_lectura"
        ]
        is False
    )


def test_empty_and_failed_runs_remain_inspectable(panel, tmp_path):
    app, _ = panel
    for state in ("sin_contenido_publicable", "sin_registros_validos", "fallido"):
        rid = "run-" + state
        _sembrar(
            str(tmp_path / "communitylab.db"),
            rid,
            con_paquete=False,
            con_analisis=state != "sin_registros_validos",
            estado=state,
        )
        click(app, "Actualizar histórico")
        next(s for s in app.selectbox if s.label == "Ejecución").set_value(rid).run()
        click(app, "Abrir ejecución")
        click(app, "2  Resultados")
        assert not app.exception
        click(app, "3  Edición y aprobación")
        assert not any(b.label == "Aprobar revisión" for b in app.button)
        click(app, "1  Carga")
