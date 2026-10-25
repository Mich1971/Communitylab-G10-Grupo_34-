"""Pruebas de la capa HTTP de curaduría y revisiones (api/rutas_curaduria.py).

Verifica paridad 1:1 con Python, autenticación X-Webhook-Secret, códigos de error
canónicos {"error", "detalle"} y modelos Pydantic v2 exactos con TestClient.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_consultas import _sembrar

from api.config import Settings
from api.main import create_app
from api.registro import RegistroIdempotencia
from storage.local_db import LocalDatabaseManager

SECRETO = "secreto-curaduria-test-12345"
H = {"X-Webhook-Secret": SECRETO}
RUN = "run-http-test"


@pytest.fixture
def cliente(tmp_path: Path) -> tuple[TestClient, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Settings(webhook_secret=SECRETO, data_dir=data_dir, _env_file=None)
    db_str = str(cfg.db_path)
    LocalDatabaseManager(db_str)
    RegistroIdempotencia(cfg.db_path).inicializar()
    _sembrar(db_str, RUN)
    app = create_app(cfg)
    return TestClient(app), db_str


# ---------------------------------------------------------------------------
# Autenticación transversal
# ---------------------------------------------------------------------------
def test_rutas_curaduria_exigen_autenticacion(cliente):
    tc, _ = cliente
    rutas = [
        ("GET", "/api/v1/runs"),
        ("GET", f"/api/v1/runs/{RUN}/detalle"),
        ("GET", f"/api/v1/runs/{RUN}/mensajes"),
        ("GET", f"/api/v1/runs/{RUN}/fuentes?source_ids=msg_001"),
        ("GET", f"/api/v1/runs/{RUN}/resumen"),
        ("GET", f"/api/v1/runs/{RUN}/rechazados"),
        ("GET", f"/api/v1/runs/{RUN}/revisiones"),
        ("GET", f"/api/v1/runs/{RUN}/revisiones/1"),
        ("POST", f"/api/v1/runs/{RUN}/revisiones"),
        ("POST", f"/api/v1/runs/{RUN}/revisiones/1/oci"),
    ]
    for metodo, ruta in rutas:
        # Sin cabecera
        res_sin = tc.request(metodo, ruta)
        assert res_sin.status_code == 401
        assert res_sin.json() == {
            "error": "no_autorizado",
            "detalle": "X-Webhook-Secret ausente o inválido",
        }
        # Con cabecera errónea
        res_bad = tc.request(metodo, ruta, headers={"X-Webhook-Secret": "clave-falsa"})
        assert res_bad.status_code == 401
        assert res_bad.json()["error"] == "no_autorizado"


# ---------------------------------------------------------------------------
# 1. listar_ejecuciones
# ---------------------------------------------------------------------------
def test_http_listar_ejecuciones(cliente):
    tc, _ = cliente
    res = tc.get("/api/v1/runs", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert any(item["run_id"] == RUN for item in data["items"])

    # Parámetro inválido
    bad = tc.get("/api/v1/runs?limite=999", headers=H)
    assert bad.status_code == 422
    assert bad.json()["error"] == "parametro_invalido"


# ---------------------------------------------------------------------------
# 2. obtener_ejecucion
# ---------------------------------------------------------------------------
def test_http_obtener_ejecucion_detalle(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/detalle", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert (data["run_id"], data["tiene_paquete"], data["revision_vigente"]) == (RUN, True, 1)

    no_existe = tc.get("/api/v1/runs/run-inexistente/detalle", headers=H)
    assert no_existe.status_code == 404
    assert no_existe.json()["error"] == "ejecucion_no_encontrada"


# ---------------------------------------------------------------------------
# 3. listar_mensajes
# ---------------------------------------------------------------------------
def test_http_listar_mensajes(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/mensajes", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data["disponible"] is True and len(data["items"]) == 2

    # Filtro
    res_apto = tc.get(f"/api/v1/runs/{RUN}/mensajes?apto=true", headers=H)
    assert res_apto.status_code == 200
    assert len(res_apto.json()["items"]) == 1

    # Parámetro inválido
    bad = tc.get(f"/api/v1/runs/{RUN}/mensajes?categoria=Invalida", headers=H)
    assert bad.status_code == 422
    assert bad.json()["error"] == "parametro_invalido"


# ---------------------------------------------------------------------------
# 4. obtener_fuentes
# ---------------------------------------------------------------------------
def test_http_obtener_fuentes(cliente):
    tc, _ = cliente
    # Soporta comas
    res = tc.get(f"/api/v1/runs/{RUN}/fuentes?source_ids=msg_001,msg_999", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["source_id"] == "msg_001"
    assert data["items"][0]["tipo_texto"] == "original_validado"
    assert data["no_encontrados"] == ["msg_999"]

    # Sin parámetros
    vacio = tc.get(f"/api/v1/runs/{RUN}/fuentes", headers=H)
    assert vacio.status_code == 422
    assert vacio.json()["error"] == "parametro_invalido"


# ---------------------------------------------------------------------------
# 5. obtener_resumen
# ---------------------------------------------------------------------------
def test_http_obtener_resumen(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/resumen", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data["disponible"] is True
    assert data["resumen"]["total_interacciones_procesadas"] == 2


# ---------------------------------------------------------------------------
# 6. listar_rechazados
# ---------------------------------------------------------------------------
def test_http_listar_rechazados(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/rechazados", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data["disponible"] is True
    assert len(data["items"]) == 1
    assert data["items"][0]["texto_crudo"] is None

    con_texto = tc.get(f"/api/v1/runs/{RUN}/rechazados?incluir_texto=true", headers=H)
    assert con_texto.status_code == 200
    assert con_texto.json()["items"][0]["texto_crudo"] == "secreto"


# ---------------------------------------------------------------------------
# 7. listar_revisiones
# ---------------------------------------------------------------------------
def test_http_listar_revisiones(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/revisiones", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert data["revision_vigente"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["numero_revision"] == 1


# ---------------------------------------------------------------------------
# 8. obtener_revision
# ---------------------------------------------------------------------------
def test_http_obtener_revision(cliente):
    tc, _ = cliente
    res = tc.get(f"/api/v1/runs/{RUN}/revisiones/1", headers=H)
    assert res.status_code == 200
    data = res.json()
    assert (data["run_id"], data["numero_revision"]) == (RUN, 1)
    assert data["paquete"]["activos"]["post_linkedin"]["titulo"] == "Titulo post"

    no_existe = tc.get(f"/api/v1/runs/{RUN}/revisiones/99", headers=H)
    assert no_existe.status_code == 404
    assert no_existe.json()["error"] == "revision_no_encontrada"


# ---------------------------------------------------------------------------
# 9. registrar_revision
# ---------------------------------------------------------------------------
def test_http_registrar_revision_exito_y_concurrencia(cliente):
    tc, _ = cliente
    payload = {
        "revision_base": 1,
        "edicion": {
            "estado": "aprobado",
            "revisor": "sergio.rebolledo",
            "comentarios": "Aprobado desde UI HTTP",
            "post_linkedin": None,
            "resumen_semanal": None,
        },
    }
    # 1. Creación exitosa (201 Created)
    res = tc.post(f"/api/v1/runs/{RUN}/revisiones", json=payload, headers=H)
    assert res.status_code == 201
    data = res.json()
    assert (data["run_id"], data["numero_revision"], data["estado"]) == (RUN, 2, "aprobado")

    # 2. Conflicto optimista al enviar de nuevo revision_base=1 (409 Conflict)
    conflicto = tc.post(f"/api/v1/runs/{RUN}/revisiones", json=payload, headers=H)
    assert conflicto.status_code == 409
    c_data = conflicto.json()
    assert c_data["error"] == "conflicto_revision"
    assert c_data["revision_actual"] == 2

    # 3. Violación de validación (aprobado sin revisor)
    invalido = tc.post(
        f"/api/v1/runs/{RUN}/revisiones",
        json={"revision_base": 2, "edicion": {"estado": "aprobado", "revisor": "  "}},
        headers=H,
    )
    assert invalido.status_code == 422
    assert invalido.json()["error"] == "revision_invalida"


# ---------------------------------------------------------------------------
# 10. guardar_revision_en_oci
# ---------------------------------------------------------------------------
def test_http_guardar_revision_en_oci(cliente):
    tc, _ = cliente
    resultado_mock = SimpleNamespace(
        bucket="communitylab-activos-marketing",
        ruta_objeto=f"activos/Semana_04/{RUN}/revision-001.json",
        status_almacenamiento="guardado_con_exito",
        comprobacion_lectura=True,
        mensaje_error=None,
        reintento_idempotente=False,
    )

    with patch("storage.revisiones.OCIStorageClient") as mock_client_cls:
        instancia = mock_client_cls.return_value
        instancia.upload_package.return_value = resultado_mock

        # Primer guardado
        res = tc.post(f"/api/v1/runs/{RUN}/revisiones/1/oci", headers=H)
        assert res.status_code == 200
        data = res.json()
        assert (data["status_almacenamiento"], data["comprobacion_lectura"]) == ("guardado_con_exito", True)
        assert data["reintento_idempotente"] is False

        # Segundo guardado: corto-circuito idempotente
        res_idem = tc.post(f"/api/v1/runs/{RUN}/revisiones/1/oci", headers=H)
        assert res_idem.status_code == 200
        assert res_idem.json()["reintento_idempotente"] is True

    # Revisión inexistente
    no_existe = tc.post(f"/api/v1/runs/{RUN}/revisiones/99/oci", headers=H)
    assert no_existe.status_code == 404
    assert no_existe.json()["error"] == "revision_no_encontrada"


# ---------------------------------------------------------------------------
# Arranque limpio: la API crea el esquema local, sin depender de que corra el worker
# ---------------------------------------------------------------------------
def test_arranque_limpio_consultas_responden_sin_worker(tmp_path: Path):
    cfg = Settings(webhook_secret=SECRETO, data_dir=tmp_path / "data", _env_file=None)
    with TestClient(create_app(cfg)) as tc:
        r = tc.get("/api/v1/runs", headers=H)
        assert r.status_code == 200
        assert r.json()["items"] == [] and r.json()["total"] == 0
        r = tc.get("/api/v1/runs/run-inexistente/detalle", headers=H)
        assert r.status_code == 404
        assert r.json()["error"] == "ejecucion_no_encontrada"
