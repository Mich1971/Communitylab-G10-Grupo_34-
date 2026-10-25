import builtins
import typing

import pytest

builtins.List = typing.List


@pytest.fixture(autouse=True)
def _aislar_ruta_de_base_de_datos(monkeypatch):
    """Un .env cargado por otro modulo fija SQLITE_DB_PATH y gana a los monkeypatch de DB_PATH:
    sin esto, varias pruebas escribirian en la base real en lugar de la temporal."""
    monkeypatch.delenv("COMMUNITYLAB_DATA_DIR", raising=False)
    monkeypatch.delenv("SQLITE_DB_PATH", raising=False)
