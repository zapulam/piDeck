import pytest
from fastapi.testclient import TestClient

from pideck.main import create_app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("PIDECK_TOKEN", raising=False)
    monkeypatch.chdir("/tmp")  # don't pick up a developer's local .env/config.yaml
    return create_app(config_path="/nonexistent/config.yaml", mock=False)


@pytest.fixture
def client(app):
    # modules without credentials just log and skip, so this is safe offline
    with TestClient(app) as c:
        yield c
