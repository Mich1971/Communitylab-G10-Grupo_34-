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


def test_panel_indica_configuracion_faltante(monkeypatch):
    from pydantic import ValidationError

    def missing():
        raise ValidationError.from_exception_data(
            "UISettings", [{"type": "missing", "loc": ("webhook_secret",), "input": {}}]
        )

    monkeypatch.setattr("ui.api_client.get_client", missing)
    app = AppTest.from_file(str(ROOT / "ui/app.py"), default_timeout=15).run()
    assert not app.exception
    assert "Configuración pendiente" in app.title[0].value
    assert app.error


def test_local_storage_idempotence_and_collision(tmp_path):
    package = {"activos": "contenido aprobado"}
    path = guardar_local(package, "activos/run/revision-001.json", tmp_path)
    assert json.loads(Path(path).read_text(encoding="utf-8")) == package
    assert guardar_local(package, "activos/run/revision-001.json", tmp_path) == path
    with pytest.raises(ValueError):
        guardar_local(
            {"activos": "otro contenido"}, "activos/run/revision-001.json", tmp_path
        )
    assert json.loads(Path(path).read_text(encoding="utf-8")) == package
