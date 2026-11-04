"""

Run piDeck in a native desktop window with an optional loopback-only server.

"""

from __future__ import annotations

from typing import Any, Callable, Iterator

import argparse
import contextlib
from contextlib import contextmanager, nullcontext
import ctypes
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import tempfile
import time
from urllib.parse import urlsplit
import urllib.request

import uvicorn

from .__main__ import ensure_token
from .config import load_config
from .main import create_app
from . import __version__
from .security import redact_logs
from hooks.pideck_hook import private_write, validate_transport_url
from .instance import desktop_instance


# --- Configuration helpers ---


def configure_taskbar() -> None:
    """
    Give Windows a stable piDeck identity before creating the native window.

    Returns:
        None: Other platforms are unchanged; Windows failures are logged.
    """
    if sys.platform != "win32":
        return
    try:
        shell32 = ctypes.WinDLL("shell32")
        set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id("piDeck.Desktop")
        if result != 0:
            raise OSError(result, "Windows rejected piDeck's taskbar identity")
    except OSError:
        logging.getLogger(__name__).warning("Could not set piDeck's taskbar identity", exc_info=True)


def window_icon() -> str:
    """
    Return the raspberry icon used for the desktop window and shortcuts.

    Returns:
        str: Absolute path to a Windows .ico or a PNG for other platforms.
    """
    name = "pideck.ico" if sys.platform == "win32" else "pideck.png"
    return str(Path(__file__).resolve().parent / "assets" / name)


def data_directory() -> Path:
    """
    Return the platform's per-user piDeck data directory without creating it.

    Returns:
        Path: A writable user-data path for config, logs, and the persistent webview profile.
    """
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "piDeck"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "piDeck"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pideck"


def validate_url(
    value: str,
    ) -> str:
    """
    Accept only HTTP(S) piDeck server URLs for the desktop viewer.

    Args:
        value (str): Address of an existing piDeck server, optionally with its token query.

    Returns:
        str: The validated address, unchanged so query parameters are preserved.

    Raises:
        ValueError: The address is not an HTTP(S) URL with a hostname.
    """
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an http:// or https:// piDeck URL without embedded login credentials.")
    validate_transport_url(value.split("#", 1)[0])
    return value


# --- Local server lifecycle ---


@contextmanager
def local_server(
    config_path: Path,
    mock: bool = False,
    port: int | None = None,
    window: dict[str, Any] | None = None,
    ) -> Iterator[str]:
    """
    Run an owned loopback server and stop it when the desktop window closes.

    Args:
        config_path (Path): Configuration path; its parent also holds the optional .env file.
        mock (bool): Whether to use demo data without creating a shared access token.
        port (int | None): Optional port override; zero selects an available port for testing.
        window (dict[str, Any] | None): Native-window holder enabling desktop setup for an owned app.

    Returns:
        Iterator[str]: Context generator yielding the loopback server URL and releasing its listener
            on exit.

    Yields:
        The local server URL once startup has completed.


    Raises:
        OSError: The requested port is unavailable.
        RuntimeError: Server startup fails or takes longer than ten seconds.
    """
    cfg = load_config(config_path)
    if not mock:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_token(str(config_path))
        ensure_token(str(config_path), "PIDECK_HOOK_TOKEN")
    app = create_app(str(config_path), mock=True if mock else None, prime=not mock, desktop=window is not None)
    if window is not None and app.state.desktop_actions is not None:
        app.state.desktop_actions.window = window
    selected_port = int(port if port is not None else cfg["server"]["port"])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # Bind before starting the worker so port conflicts are reported in the main thread.
        listener.bind(("127.0.0.1", selected_port))
        listener.listen(128)
        actual_port = listener.getsockname()[1]
        app.state.prime_url = f"http://127.0.0.1:{actual_port}/"
        if app.state.desktop_actions is not None:
            app.state.desktop_actions.url = app.state.prime_url
            app.state.access.activation = window.get("activation", "")
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=actual_port, log_config=None,
            access_log=False, timeout_graceful_shutdown=3,
            proxy_headers=False, ws_max_size=1024, limit_concurrency=64,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("piDeck's local server could not start. See the desktop log for details.")
                time.sleep(0.02)
            if window is not None and window.get("instance_path"):
                private_write(window["instance_path"], json.dumps({"url": app.state.prime_url,
                              "activation": window["activation"]}))
            yield f"http://127.0.0.1:{actual_port}/"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


# --- Packaged backend self-test ---


def self_test(
    report: Path,
    ) -> None:
    """
    Verify the desktop backend and UI assets without opening a native window.

    Args:
        report (Path): Destination for a JSON success report used by automated checks.

    Returns:
        None: No value is returned.

    Raises:
        ImportError: The optional webview dependency is unavailable.
        RuntimeError: A backend or UI asset check fails.
    """
    import webview  # noqa: F401 -- confirm the optional desktop dependency is importable

    with tempfile.TemporaryDirectory(prefix="pideck-self-test-") as temp:
        with local_server(Path(temp) / "config.yaml", mock=True, port=0) as url:
            for asset in ("", "app.js", "style.css", "creature.js"):
                with urllib.request.urlopen(url + asset, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"Missing packaged UI asset: {asset}")
            with urllib.request.urlopen(url + "api/health", timeout=5) as response:
                if not json.load(response).get("ok"):
                    raise RuntimeError("Packaged backend health check failed")
            request = urllib.request.Request(url + "api/agents/event", data=json.dumps({
                "session_id": "self-test", "state": "working", "agent": "cursor",
            }).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                if json.load(response)["agent"]["agent"] != "cursor":
                    raise RuntimeError("Packaged agent event check failed")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"ok": True, "version": __version__, "platform": sys.platform,
                                 "checks": ["desktop-import", "ui-assets", "health", "agent-event", "shutdown"]}),
                      encoding="utf-8")


# --- Native window and entry point ---


def native_self_test(
    report: Path,
    ) -> None:
    """
    Verify real native-window authentication using isolated configuration and no live accounts.

    Args:
        report (Path): Destination for the non-secret native-window success report.

    Returns:
        None: The hidden window is closed and its temporary vault credentials are removed.
    """
    import webview
    from unittest.mock import patch
    from .credentials import NAMES, SecretStore

    errors = []
    ready = threading.Event()
    original_create, original_start = webview.create_window, webview.start

    def create_hidden(
        *args: Any,
        **kwargs: Any,
        ) -> Any:
        """
        Create the production native window without displaying it during automated validation.

        Args:
            *args (Any): Native window creation arguments.
            **kwargs (Any): Native window creation options.

        Returns:
            Any: Real pywebview window using a hidden native renderer.
        """
        return original_create(*args, **kwargs, hidden=True)

    def inspect() -> None:
        """
        Wait for the authenticated WebSocket snapshot and validate the native window.

        Returns:
            None: A result is written or a captured error is raised after the window closes.
        """
        window = webview.windows[0]
        try:
            if not window.events.loaded.wait(40):
                raise RuntimeError("Native page did not load")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                window.run_js("if (S.initialized && document.querySelector('#login').hidden && !location.hash) "
                              "fetch('/api/desktop/native-check', authInit({method:'POST'}));")
                if ready.wait(0.1):
                    break
            else:
                raise RuntimeError("Native display authentication failed")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({"ok": True, "platform": sys.platform,
                              "checks": ["native-window", "private-session", "websocket-snapshot", "bootstrap-erased"]}), encoding="utf-8")
        except BaseException as error:
            errors.append(error)
        finally:
            window.destroy()

    with tempfile.TemporaryDirectory(prefix="pideck-native-check-", ignore_cleanup_errors=True) as temporary:
        config = Path(temporary) / "config.yaml"
        private_write(config, "modules:\n  agents:\n    enabled: false\n  spotify:\n    enabled: false\n  slack:\n    enabled: false\n")
        store = SecretStore(config)
        env = {key: value for key, value in os.environ.items() if key not in NAMES}
        with patch.dict(os.environ, env, clear=True), patch.object(webview, "create_window", create_hidden), \
                patch("pideck.desktop.data_directory", return_value=Path(temporary) / "profile"), \
                patch.object(webview, "start", lambda **kwargs: original_start(inspect, **kwargs)):
            try:
                run_desktop(config_path=config, port=0, native_test=ready.set)
            finally:
                if store.vault:
                    for name in ("PIDECK_TOKEN", "PIDECK_HOOK_TOKEN"):
                        with contextlib.suppress(Exception):
                            store.vault.delete_password(store.service, name)
    if errors:
        raise errors[0]


def run_desktop(
    config_path: Path | None = None,
    mock: bool = False,
    port: int | None = None,
    url: str | None = None,
    on_top: bool = False,
    native_test: Callable[[], None] | None = None,
    ) -> None:
    """
    Open a resizable native window on Windows or macOS using the existing piDeck UI.

    Args:
        config_path (Path | None): Local server config; defaults to the user's piDeck data
            directory.
        mock (bool): Whether a locally hosted server should show demo data.
        port (int | None): Local server port override; ignored for an existing server URL.
        url (str | None): Existing piDeck address, or None to start a local server.
        on_top (bool): Whether to keep the window above other windows.
        native_test (Callable[[], None] | None): Isolated native-test callback, absent in normal runs.

    Returns:
        None: No value is returned.

    Raises:
        RuntimeError: The optional desktop dependency is missing or startup fails.
        ValueError: The existing-server URL is invalid.
        OSError: The configured local server port is unavailable.
    """
    if url is not None:
        validate_url(url)
    configure_taskbar()
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("Install desktop dependencies first: python -m pip install -r requirements-desktop.txt") from exc
    storage = data_directory()
    storage.mkdir(parents=True, exist_ok=True)
    config = config_path or storage / "config.yaml"
    ownership = nullcontext({}) if url is not None else desktop_instance(config)
    with ownership as window:
        if window is None:
            return
        if native_test is not None:
            window["native_check"] = native_test
        source = nullcontext(url) if url is not None else local_server(config, mock, port, window=window)
        with source as address:
            if url is None and not mock:
                address += "#token=" + os.environ["PIDECK_TOKEN"]
            window["native"] = webview.create_window("piDeck", address, width=520, height=720,
                                                     min_size=(360, 400), on_top=on_top, background_color="#0b0d12")
            webview.start(private_mode=False, storage_path=str(storage / "webview"), icon=window_icon())


def main(
    argv: list[str] | None = None,
    ) -> int:
    """
    Parse desktop options and open the window, recording startup errors for GUI launches.

    Args:
        argv (list[str] | None): Command-line arguments, or None to use sys.argv.

    Returns:
        int: Zero after the window closes, or one if startup failed.
    """
    parser = argparse.ArgumentParser(description="Open piDeck in a small desktop window.")
    parser.add_argument("--config", type=Path, help="local config.yaml path")
    parser.add_argument("--mock", action="store_true", help="show demo data")
    parser.add_argument("--port", type=int, help="local port (default 8765)")
    parser.add_argument("--url", help="open an existing piDeck server instead of running one")
    parser.add_argument("--on-top", action="store_true", help="keep the window above other windows")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-window", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_window:
        if args.report is None:
            parser.error("--smoke-window requires --report")
        native_self_test(args.report)
        return 0
    if args.self_test:
        if args.report is None:
            parser.error("--self-test requires --report")
        self_test(args.report)
        return 0
    if args.report is not None:
        parser.error("--report requires --self-test")
    if args.url and (args.config or args.mock or args.port is not None):
        parser.error("--url cannot be combined with local server options")
    if args.config is None and not args.url and not getattr(sys, "frozen", False) and Path("config.yaml").is_file():
        args.config = Path("config.yaml").resolve()
    storage = data_directory()
    storage.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False) and not args.url:
        config = args.config or storage / "config.yaml"
        if not config.is_file():
            from .config import DEFAULTS
            import yaml

            private_write(config, yaml.safe_dump(DEFAULTS, sort_keys=False))
        example = config.parent / ".env.example"
        if not example.is_file():
            template = Path(__file__).resolve().parents[1] / ".env.example"
            private_write(example, template.read_text(encoding="utf-8"))
        args.config = config
    logging.basicConfig(filename=storage / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redact_logs()
    try:
        run_desktop(args.config, args.mock, args.port, args.url, args.on_top)
        return 0
    except Exception as exc:
        # Prefer the exception type in logs when the message may include a remote token query.
        detail = type(exc).__name__ if "?" in str(exc) else f"{type(exc).__name__}: {exc}"
        logging.error("Desktop startup failed (%s)", detail)
        message = f"Could not open piDeck: {exc}\n\nLog: {storage / 'desktop.log'}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        try:
            from tkinter import Tk, messagebox
            root = Tk()
            root.withdraw()
            messagebox.showerror("piDeck", message)
            root.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
