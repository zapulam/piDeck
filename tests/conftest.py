import pytest
from fastapi.testclient import TestClient

from pideck.main import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app(
        monkeypatch,
        tmp_path,
):
    """Create an isolated app with test authentication and no developer configuration.

    Args:
        monkeypatch: Pytest helper for temporary environment and directory changes.
        tmp_path: Platform-independent temporary working directory.

    Returns:
        A FastAPI application with real sources and no configured external accounts.
    """
    monkeypatch.setenv("PIDECK_TOKEN", TOKEN)
    monkeypatch.chdir(tmp_path)  # don't pick up a developer's local .env/config.yaml
    return create_app(config_path="/nonexistent/config.yaml", mock=False)


@pytest.fixture
def client(app):
    # modules without credentials just log and skip, so this is safe offline
    with TestClient(app, headers=AUTH) as c:
        yield c
