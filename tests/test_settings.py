"""

Saved bottom-bar pages, stored beside config and kept out of git.

"""

from __future__ import annotations

from pathlib import Path
from fastapi.testclient import TestClient

import json

from pideck.config import PAGES


# --- Page selection file ---


def test_ui_settings_override_config_and_ignore_unknown_pages(
    client: TestClient,
    tmp_path: Path,
    ) -> None:
    """
    A saved selection replaces config.yaml views and drops ids the UI does not have.

    Args:
        client (TestClient): Authenticated test client for the isolated application.
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert client.get("/api/config").json()["views"] == list(PAGES)
    response = client.post("/api/ui-settings", json={"views": ["slack", "nope", "agents"]})
    assert response.status_code == 200
    assert response.json()["views"] == ["agents", "slack"]
    assert client.get("/api/config").json()["views"] == ["agents", "slack"]
    assert json.loads((tmp_path / "ui-settings.json").read_text(encoding="utf-8"))["views"] == ["agents", "slack"]
    assert client.post("/api/ui-settings", json={"views": []}).json()["views"] == []
    assert client.post("/api/ui-settings", json={"views": "agents"}).status_code == 422
