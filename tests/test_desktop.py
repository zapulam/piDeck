"""Desktop server lifecycle and window configuration, independent of native GUI libraries."""
from contextlib import contextmanager
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest

from pideck import desktop


"""# --- URL and data-directory helpers ---"""


@pytest.mark.parametrize("url", ["file:///tmp/test", "javascript:alert(1)", "https://", "https://user:pass@pi/"])
def test_desktop_rejects_non_server_urls(
        url,
):
    """Only HTTP(S) server addresses without embedded login credentials are accepted."""
    with pytest.raises(ValueError):
        desktop.validate_url(url)


@pytest.mark.parametrize("platform, suffix", [
    ("win32", "AppData/Local/piDeck"), ("darwin", "Library/Application Support/piDeck"),
    ("linux", ".local/share/pideck"),
])
def test_desktop_data_directory_is_per_user(
        monkeypatch,
        tmp_path,
        platform,
        suffix,
):
    """Packaged apps store mutable files under the user profile rather than the app bundle."""
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert desktop.data_directory() == tmp_path / suffix


"""# --- Real local server lifecycle ---"""


def test_desktop_local_server_serves_ui_and_releases_port(
        tmp_path,
):
    """Run the actual backend on loopback, verify assets and auth, then verify shutdown."""
    with desktop.local_server(tmp_path / "config.yaml", mock=True, port=0) as url:
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("api/health").json()["ok"]
            assert "piDeck" in client.get("/").text
            assert client.get("app.js").status_code == 200
            assert client.post("api/agents/event", json={"session_id": "c", "state": "working", "agent": "cursor"}).status_code == 200
            assert client.post("api/agents/event", json={"session_id": "c", "state": "working"},
                               headers={"Origin": "https://untrusted.example"}).status_code == 403
    with httpx.Client(trust_env=False, timeout=1) as client:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            client.get(url + "api/health")


def test_desktop_reports_port_conflicts(
        tmp_path,
):
    """An occupied port is reported rather than silently opening an unrelated server."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            with desktop.local_server(tmp_path / "config.yaml", mock=True, port=listener.getsockname()[1]):
                pytest.fail("An occupied port should not start")


"""# --- Native-window orchestration ---"""


def test_desktop_remote_window_does_not_start_a_server(
        monkeypatch,
        tmp_path,
):
    """Remote viewer mode preserves authentication queries and starts only the native window."""
    calls = []
    fake = SimpleNamespace(create_window=lambda *args, **kwargs: calls.append((args, kwargs)),
                           start=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "data_directory", lambda: tmp_path)

    def unexpected_server(
            *args,
            **kwargs,
    ):
        """Fail if viewer mode attempts to create a backend."""
        pytest.fail("Viewer mode must not start a backend")

    monkeypatch.setattr(desktop, "local_server", unexpected_server)
    url = "https://pideck.example/?token=example"
    desktop.run_desktop(url=url, on_top=True)
    assert calls[0][0] == ("piDeck", url)
    assert calls[0][1]["on_top"] is True
    assert calls[0][1]["width"] == 520 and calls[0][1]["height"] == 720
    assert calls[1]["private_mode"] is False
    assert Path(calls[1]["icon"]).is_file()
    assert calls[1]["icon"].endswith("pideck.ico" if sys.platform == "win32" else "pideck.png")


def test_desktop_cli_uses_repo_config_and_env(
        monkeypatch,
        tmp_path,
):
    """Launching from a checkout uses that folder's config.yaml, which loads the repo .env."""
    seen = {}
    (tmp_path / "config.yaml").write_text("server:\n  port: 8765\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(desktop, "run_desktop", lambda *args, **kwargs: seen.update(config=args[0]))
    assert desktop.main([]) == 0
    assert seen["config"] == (tmp_path / "config.yaml").resolve()
    other = tmp_path / "empty"
    other.mkdir()
    monkeypatch.chdir(other)
    assert desktop.main([]) == 0
    assert seen["config"] is None


def test_desktop_closes_owned_server_when_window_start_fails(
        monkeypatch,
        tmp_path,
):
    """A failed native renderer must still close the local backend context."""
    lifecycle = []

    @contextmanager
    def server(
            *args,
            **kwargs,
    ):
        """Record entry and cleanup of the owned backend context."""
        lifecycle.append("start")
        try:
            yield "http://127.0.0.1:8765/"
        finally:
            lifecycle.append("stop")

    def failed_window(
            *args,
            **kwargs,
    ):
        """Simulate a native renderer failure before its event loop starts."""
        raise RuntimeError("Renderer unavailable")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=failed_window))
    monkeypatch.setattr(desktop, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(desktop, "local_server", server)
    with pytest.raises(RuntimeError, match="Renderer unavailable"):
        desktop.run_desktop(mock=True)
    assert lifecycle == ["start", "stop"]
