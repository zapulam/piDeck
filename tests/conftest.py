"""

Shared isolated application and authenticated HTTP client fixtures.

"""

from __future__ import annotations

from typing import Iterator
from pathlib import Path
from fastapi import FastAPI

import pytest
from fastapi.testclient import TestClient

from pideck.main import create_app

# --- Tests ---

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> FastAPI:
    """
    Create an isolated app with test authentication and no developer configuration.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper for temporary environment and directory
            changes.
        tmp_path (Path): Platform-independent temporary working directory.

    Returns:
        FastAPI: A FastAPI application with real sources and no configured external accounts.
    """
    monkeypatch.setenv("PIDECK_TOKEN", TOKEN)
    monkeypatch.chdir(tmp_path)  # don't pick up a developer's local .env/config.yaml
    return create_app(config_path="/nonexistent/config.yaml", mock=False)


@pytest.fixture
def client(
    app: FastAPI,
    ) -> Iterator[TestClient]:
    # modules without credentials just log and skip, so this is safe offline
    """
    Provide an authenticated HTTP client for the isolated application.

    Args:
        app (FastAPI): Isolated FastAPI application used by this operation.

    Returns:
        Iterator[TestClient]: Authenticated client yielded while the test application lifespan is
            active.
    """
    with TestClient(app, headers=AUTH) as c:
        yield c
