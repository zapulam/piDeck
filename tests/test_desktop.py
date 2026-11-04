"""

Desktop server lifecycle and window configuration, independent of native GUI libraries.

"""

from __future__ import annotations

from typing import Any, Iterator

from contextlib import contextmanager
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest

from pideck import desktop


# --- URL and data-directory helpers ---


@pytest.mark.parametrize("url", ["file:///tmp/test", "javascript:alert(1)", "https://", "https://user:pass@pi/"])
def test_desktop_rejects_non_server_urls(
    url: str,
    ) -> None:
    """
    Only HTTP(S) server addresses without embedded login credentials are accepted.

    Args:
        url (str): HTTP or HTTPS address of the piDeck display.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with pytest.raises(ValueError):
        desktop.validate_url(url)


@pytest.mark.parametrize("platform, suffix", [
    ("win32", "AppData/Local/piDeck"), ("darwin", "Library/Application Support/piDeck"),
    ("linux", ".local/share/pideck"),
])
def test_desktop_data_directory_is_per_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    suffix: str,
    ) -> None:
    """
    Packaged apps store mutable files under the user profile rather than the app bundle.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        platform (str): Platform identifier used to simulate operating-system behavior.
        suffix (str): Expected platform-specific directory suffix.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert desktop.data_directory() == tmp_path / suffix


# --- Real local server lifecycle ---


def test_desktop_local_server_serves_ui_and_releases_port(
    tmp_path: Path,
    ) -> None:
    """
    Run the actual backend on loopback, verify assets and auth, then verify shutdown.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
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
    tmp_path: Path,
    ) -> None:
    """
    An occupied port is reported rather than silently opening an unrelated server.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            with desktop.local_server(tmp_path / "config.yaml", mock=True, port=listener.getsockname()[1]):
                pytest.fail("An occupied port should not start")


# --- Native-window orchestration ---


@pytest.mark.parametrize("platform, result", [("win32", 0), ("win32", -1), ("linux", 0), ("darwin", 0)])
def test_taskbar_identity_is_windows_only_and_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    platform: str,
    result: int,
    ) -> None:
    """
    Set the Windows identity while keeping other platforms and failed calls usable.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper restoring patched platform and native APIs.
        caplog (pytest.LogCaptureFixture): Captured warnings from failed native calls.
        platform (str): Operating system to simulate.
        result (int): HRESULT returned by the simulated Windows API.

    Returns:
        None: Assertions confirm the platform guard, identity, and error handling.
    """
    from unittest.mock import Mock

    set_app_id = Mock(return_value=result)
    library = Mock(return_value=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=set_app_id))
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setattr(desktop.ctypes, "WinDLL", library, raising=False)
    desktop.configure_taskbar()
    if platform == "win32":
        library.assert_called_once_with("shell32")
        set_app_id.assert_called_once_with("piDeck.Desktop")
        assert set_app_id.argtypes == [desktop.ctypes.c_wchar_p]
        assert set_app_id.restype == desktop.ctypes.c_long
        assert ("Could not set piDeck's taskbar identity" in caplog.text) == (result != 0)
    else:
        library.assert_not_called()


def test_desktop_remote_window_does_not_start_a_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> None:
    """
    Remote viewer mode preserves authentication queries and starts only the native window.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    calls = []
    identity = []
    fake = SimpleNamespace(create_window=lambda *args, **kwargs: calls.append((args, kwargs)),
                           start=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(desktop, "configure_taskbar", lambda: identity.append(len(calls)))

    def unexpected_server(
        *args: Any,
        **kwargs: Any,
        ) -> None:
        """
        Fail if viewer mode attempts to create a backend.

        Args:
            *args (Any): Additional arguments accepted by this callback or forwarded to the wrapped
                operation.
            **kwargs (Any): Additional arguments accepted by this callback or forwarded to the
                wrapped operation.

        Returns:
            None: Assertions or fixture setup completed successfully.
        """
        pytest.fail("Viewer mode must not start a backend")

    monkeypatch.setattr(desktop, "local_server", unexpected_server)
    url = "https://pideck.example/?token=example"
    desktop.run_desktop(url=url, on_top=True)
    assert identity == [0]
    assert calls[0][0] == ("piDeck", url)
    assert calls[0][1]["on_top"] is True
    assert calls[0][1]["width"] == 520 and calls[0][1]["height"] == 720
    assert calls[1]["private_mode"] is False
    assert Path(calls[1]["icon"]).is_file()
    assert calls[1]["icon"].endswith("pideck.ico" if sys.platform == "win32" else "pideck.png")


def test_desktop_cli_uses_repo_config_and_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> None:
    """
    Launching from a checkout uses that folder's config.yaml, which loads the repo .env.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> None:
    """
    A failed native renderer must still close the local backend context.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    lifecycle = []

    @contextmanager
    def server(
        *args: Any,
        **kwargs: Any,
        ) -> Iterator[str]:
        """
        Record entry and cleanup of the owned backend context.

        Args:
            *args (Any): Additional arguments accepted by this callback or forwarded to the wrapped
                operation.
            **kwargs (Any): Additional arguments accepted by this callback or forwarded to the
                wrapped operation.

        Returns:
            Iterator[str]: Iterator providing the documented values for the lifetime of this
                operation.
        """
        lifecycle.append("start")
        try:
            yield "http://127.0.0.1:8765/"
        finally:
            lifecycle.append("stop")

    def failed_window(
        *args: Any,
        **kwargs: Any,
        ) -> None:
        """
        Simulate a native renderer failure before its event loop starts.

        Args:
            *args (Any): Additional arguments accepted by this callback or forwarded to the wrapped
                operation.
            **kwargs (Any): Additional arguments accepted by this callback or forwarded to the
                wrapped operation.

        Returns:
            None: Assertions or fixture setup completed successfully.
        """
        raise RuntimeError("Renderer unavailable")

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=failed_window))
    monkeypatch.setattr(desktop, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(desktop, "local_server", server)
    with pytest.raises(RuntimeError, match="Renderer unavailable"):
        desktop.run_desktop(mock=True)
    assert lifecycle == ["start", "stop"]
