"""Run piDeck in a native desktop window with an optional loopback-only server."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
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


"""# --- Configuration helpers ---"""


def data_directory() -> Path:
    """Return the platform's per-user piDeck data directory without creating it.

    Returns:
        A writable user-data path for config, logs, and the persistent webview profile.
    """
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "piDeck"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "piDeck"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pideck"


def validate_url(
        value: str,
) -> str:
    """Accept only HTTP(S) piDeck server URLs for the desktop viewer.

    Args:
        value: Address of an existing piDeck server, optionally with its token query.

    Returns:
        The validated address, unchanged so query parameters are preserved.

    Raises:
        ValueError: The address is not an HTTP(S) URL with a hostname.
    """
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an http:// or https:// piDeck URL without embedded login credentials.")
    return value


"""# --- Local server lifecycle ---"""


@contextmanager
def local_server(
        config_path: Path,
        mock: bool = False,
        port: int | None = None,
):
    """Run an owned loopback server and stop it when the desktop window closes.

    Args:
        config_path: Configuration path; its parent also holds the optional .env file.
        mock: Whether to use demo data without creating a shared access token.
        port: Optional port override; zero selects an available port for testing.

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
    app = create_app(str(config_path), mock=True if mock else None)
    selected_port = int(port if port is not None else cfg["server"]["port"])
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # Bind before starting the worker so port conflicts are reported in the main thread.
        listener.bind(("127.0.0.1", selected_port))
        listener.listen(128)
        actual_port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=actual_port, log_config=None,
            access_log=False, timeout_graceful_shutdown=3,
        ))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("piDeck's local server could not start. See the desktop log for details.")
                time.sleep(0.02)
            yield f"http://127.0.0.1:{actual_port}/"
        finally:
            server.should_exit = True
            thread.join(timeout=5)


"""# --- Packaged backend self-test ---"""


def self_test(
        report: Path,
) -> None:
    """Verify a frozen backend and its bundled UI without opening a native window.

    Args:
        report: Destination for a JSON success report read by the CI smoke-test runner.

    Raises:
        ImportError: The packaged webview dependency is unavailable.
        RuntimeError: A backend or bundled asset check fails.
    """
    import webview  # noqa: F401 -- the frozen package must contain its optional desktop dependency

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


"""# --- Native window and entry point ---"""


def run_desktop(
        config_path: Path | None = None,
        mock: bool = False,
        port: int | None = None,
        url: str | None = None,
        on_top: bool = False,
) -> None:
    """Open a resizable native window on Windows or macOS using the existing piDeck UI.

    Args:
        config_path: Local server config; defaults to the user's piDeck data directory.
        mock: Whether a locally hosted server should show demo data.
        port: Local server port override; ignored for an existing server URL.
        url: Existing piDeck address, or None to start a local server.
        on_top: Whether to keep the window above other windows.

    Raises:
        RuntimeError: The optional desktop dependency is missing or startup fails.
        ValueError: The existing-server URL is invalid.
        OSError: The configured local server port is unavailable.
    """
    if url is not None:
        validate_url(url)
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("Install desktop dependencies first: python -m pip install -r requirements-desktop.txt") from exc
    storage = data_directory()
    storage.mkdir(parents=True, exist_ok=True)
    config = config_path or storage / "config.yaml"
    source = nullcontext(url) if url is not None else local_server(config, mock, port)
    with source as address:
        webview.create_window("piDeck", address, width=520, height=720,
                              min_size=(360, 400), on_top=on_top, background_color="#0b0d12")
        webview.start(private_mode=False, storage_path=str(storage / "webview"))


def main(
        argv: list[str] | None = None,
) -> int:
    """Parse desktop options and open the window, recording startup errors for GUI launches.

    Args:
        argv: Command-line arguments, or None to use sys.argv.

    Returns:
        Zero after the window closes, or one if startup failed.
    """
    parser = argparse.ArgumentParser(description="Open piDeck in a small desktop window.")
    parser.add_argument("--config", type=Path, help="local config.yaml path")
    parser.add_argument("--mock", action="store_true", help="show demo data")
    parser.add_argument("--port", type=int, help="local port (default 8765)")
    parser.add_argument("--url", help="open an existing piDeck server instead of running one")
    parser.add_argument("--on-top", action="store_true", help="keep the window above other windows")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        if args.report is None:
            parser.error("--self-test requires --report")
        self_test(args.report)
        return 0
    if args.report is not None:
        parser.error("--report requires --self-test")
    if args.url and (args.config or args.mock or args.port is not None):
        parser.error("--url cannot be combined with local server options")
    storage = data_directory()
    storage.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=storage / "desktop.log", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        run_desktop(args.config, args.mock, args.port, args.url, args.on_top)
        return 0
    except Exception as exc:
        # Avoid logging exception text that might contain a remote URL's token query.
        logging.error("Desktop startup failed (%s)", type(exc).__name__)
        message = f"Could not open piDeck: {exc}\n\nLog: {storage / 'desktop.log'}"
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        else:
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
