"""
Pruebas del server. Se corren desde la raíz de tecnomonitor-server con:

    python3 -m pytest tests

Igual que server.py, ponen dashboard_app/ en sys.path: los módulos se importan
como top-level (`alerts_engine`, `datos`, `routers`...). Cada prueba usa una
base SQLite en memoria con el esquema de database.py.
"""
import os
import sys
from datetime import datetime

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "dashboard_app"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import database  # noqa: E402


@pytest.fixture
def db():
    eng = create_engine("sqlite://")
    database.Base.metadata.create_all(eng)
    sesion = sessionmaker(bind=eng)()
    yield sesion
    sesion.close()


@pytest.fixture
def sin_asana(monkeypatch):
    """Los detectores no llaman a Asana ni al websocket del dashboard."""
    from alerts_engine import estado, modulos
    monkeypatch.setattr(estado, "asana_conector", None)
    monkeypatch.setattr(estado.requests, "post", lambda *a, **k: None)
    monkeypatch.setattr(modulos, "_avisar_ws", lambda: None)


@pytest.fixture
def ahora():
    return datetime.now()
