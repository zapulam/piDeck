"""

Check UI upgrades invalidate cached assets even when release versions and file metadata match.

"""

import hashlib
import os
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from pideck import main


# --- Cache headers and same-version upgrades ---


def test_ui_entrypoints_and_assets_require_fresh_content(
    client: TestClient,
    ) -> None:
    """
    Serve fresh fingerprinted entrypoints and revalidate scripts even on conditional responses.

    Args:
        client (TestClient): Application client with isolated source state.

    Returns:
        None: Both entrypoints use current content URLs and static cache policies are explicit.
    """
    for entry in ("/", "/index.html"):
        response = client.get(entry)
        assert response.headers["cache-control"] == "no-store"
        for name in ("app.js", "creature.js", "style.css"):
            digest = hashlib.sha256((main.UI_DIR / name).read_bytes()).hexdigest()[:16]
            resource = f"{name}?v={digest}"
            assert resource in response.text
            asset = client.get("/" + resource)
            assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"
            unchanged = client.get("/" + resource, headers={"If-None-Match": asset.headers["etag"]})
            assert unchanged.status_code == 304 and unchanged.headers["cache-control"] == "no-cache"


def test_same_version_upgrade_changes_asset_url_despite_identical_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> None:
    """
    Replace a cached bundle while preserving size and timestamps and require a new asset URL.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper restoring the real packaged UI directory.
        tmp_path (Path): Isolated minimal UI and server configuration.

    Returns:
        None: New HTML bypasses the legacy asset cache despite an unchanged static ETag.
    """
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<script src="app.js"></script>')
    (ui / "style.css").write_text("body {}")
    (ui / "creature.js").write_text("// sprites")
    script = ui / "app.js"
    script.write_text('window.pageOrder = "old";')
    original = script.stat()
    monkeypatch.setattr(main, "UI_DIR", ui)
    with TestClient(main.create_app(str(tmp_path / "config.yaml"), mock=True)) as browser:
        before = re.search(r'src="([^"]+)"', browser.get("/").text)[1]
        stale = browser.get("/" + before)
        script.write_text('window.pageOrder = "new";')
        os.utime(script, ns=(original.st_atime_ns, original.st_mtime_ns))
        after = re.search(r'src="([^"]+)"', browser.get("/").text)[1]
        assert before != after
        current = browser.get("/" + after)
        assert current.headers["etag"] == stale.headers["etag"]
        assert '"new"' in current.text and '"old"' in stale.text
