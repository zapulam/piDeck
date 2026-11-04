"""

Expose authenticated desktop setup actions without a shell or a system Python dependency.

"""

from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time
from typing import Any
import webbrowser

from hooks.pideck_hook import private_write
from .credentials import SecretStore
from .sources.spotify import Spotify
from .spotify_auth import authorization, callback_code, exchange_code


# --- Per-user startup registration ---


def startup_enabled() -> bool:
    """
    Read this user's piDeck startup registration without changing OS settings.

    Returns:
        bool: True when piDeck has a per-user startup registration.
    """
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "piDeck")
            return True
        except FileNotFoundError:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library/LaunchAgents/com.zapulam.pideck.plist").is_file()
    return False


def set_startup(
    enabled: bool,
    config: Path,
    ) -> None:
    """
    Register or remove the installed application for this user's next sign-in.

    Args:
        enabled (bool): Whether piDeck should start when this user signs in.
        config (Path): User-owned configuration to reopen at sign-in.

    Returns:
        None: Per-user startup settings are updated without administrator rights.
    """
    if not getattr(sys, "frozen", False):
        raise ValueError("Startup settings are available in the installed desktop application")
    command = [sys.executable, "--config", str(config.resolve())]
    if sys.platform == "darwin" and enabled and (str(sys.executable).startswith("/Volumes/") or "AppTranslocation" in Path(sys.executable).parts):
        raise ValueError("Move piDeck to Applications and open it there before enabling startup")
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            if enabled:
                winreg.SetValueEx(key, "piDeck", 0, winreg.REG_SZ, subprocess.list2cmdline(command))
            else:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, "piDeck")
    elif sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents/com.zapulam.pideck.plist"
        if enabled:
            content = plistlib.dumps({"Label": "com.zapulam.pideck", "ProgramArguments": command,
                                     "RunAtLoad": True, "LimitLoadToSessionType": "Aqua"}).decode()
            private_write(path, content)
        else:
            path.unlink(missing_ok=True)
    else:
        raise ValueError("Desktop startup is supported on Windows and macOS")


# --- Authenticated desktop setup ---


class DesktopActions:
    """Perform only the fixed setup actions available to a local native display."""

    def __init__(
        self,
        config: Path,
        spotify: Spotify,
        ) -> None:
        """
        Initialize configuration ownership and bounded Spotify setup state.

        Args:
            config (Path): User-owned application configuration file.
            spotify (Spotify): Playback integration receiving a newly authorized refresh token.

        Returns:
            None: Desktop actions are ready; no startup or account settings change yet.
        """
        self.config, self.spotify = config.resolve(), spotify
        self.url = ""
        self.window: dict[str, Any] = {}
        self.spotify_status = "Not connected"
        self._oauth_thread: threading.Thread | None = None
        self._oauth_server: HTTPServer | None = None
        self._lock = threading.Lock()

    def status(
        self,
        ) -> dict[str, Any]:
        """
        Return public setup status without account identifiers or credentials.

        Returns:
            dict[str, Any]: Startup and Spotify connection state for Settings.
        """
        return {"startup": startup_enabled(), "spotify": self.spotify_status,
                "packaged": bool(getattr(sys, "frozen", False))}

    def perform(
        self,
        action: str,
        ) -> dict[str, Any]:
        """
        Execute an allowlisted setup action for an authenticated native-app request.

        Args:
            action (str): Fixed action name supplied by the Settings page.

        Returns:
            dict[str, Any]: Updated public setup status after the action is started.
        """
        if action == "open-config":
            directory = str(self.config.parent)
            if sys.platform == "win32":
                os.startfile(directory)
            elif sys.platform == "darwin":
                subprocess.run(["open", directory], check=True)
            else:
                raise ValueError("Open the configuration directory in your file manager")
        elif action in {"startup-on", "startup-off"}:
            set_startup(action == "startup-on", self.config)
        elif action == "connect-agents":
            from hooks.install import main as install_hooks

            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = install_hooks(["--url", self.url, "--token", os.environ["PIDECK_HOOK_TOKEN"], "--no-check"])
            if result:
                raise ValueError("Install Claude Code, Codex, or Cursor first, then connect again")
        elif action == "spotify-connect":
            self.connect_spotify()
        elif action == "activate" and self.window.get("native") is not None:
            self.window["native"].show()
            self.window["native"].restore()
        else:
            raise ValueError("Unsupported desktop action")
        return self.status()

    def connect_spotify(
        self,
        ) -> None:
        """
        Open Spotify authorization with one short-lived loopback callback listener.

        Returns:
            None: The browser is opened or a safe setup error is raised.
        """
        with self._lock:
            if self._oauth_thread and self._oauth_thread.is_alive():
                raise ValueError("A Spotify connection attempt is already open")
            store = SecretStore(self.config)
            store.load()
            client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
            if not client_id:
                raise ValueError("Add SPOTIFY_CLIENT_ID to .env in the configuration folder, then connect again")
            redirect = "http://127.0.0.1:8888/callback"
            url, state, verifier = authorization(client_id, redirect)
            owner = self
            deadline = time.monotonic() + 180

            class Callback(BaseHTTPRequestHandler):
                """Validate the one-time browser callback without logging codes or tokens."""

                timeout = 3

                def do_GET(
                    self,
                    ) -> None:
                    """
                    Validate this attempt's callback and save its refresh token privately.

                    Returns:
                        None: A generic, non-cacheable browser response is sent.
                    """
                    try:
                        if time.monotonic() >= deadline:
                            raise ValueError("Expired authorization")
                        code = callback_code("http://127.0.0.1:8888" + self.path, redirect, state)
                        token = exchange_code(code, client_id, redirect, verifier)
                        store.set("SPOTIFY_REFRESH_TOKEN", token)
                        owner.spotify.client_id = client_id
                        owner.spotify.refresh_token = token
                        owner.spotify._access = None
                        owner.spotify_status = "Connected"
                        status, body = 200, b"Spotify connected. You can close this tab."
                    except Exception:
                        status, body = 400, b"Spotify connection failed. Start again from piDeck."
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(
                    self,
                    format: str,
                    *args: Any,
                    ) -> None:
                    """
                    Suppress HTTP callback logging because request URLs contain OAuth codes.

                    Args:
                        format (str): Ignored HTTP log format.
                        *args (Any): Ignored request details containing private authorization data.

                    Returns:
                        None: No callback data is logged.
                    """
                    return None

            server = HTTPServer(("127.0.0.1", 8888), Callback)
            server.timeout = 1
            self._oauth_server = server
            self.spotify_status = "Waiting for Spotify approval"

            def receive_callback() -> None:
                """
                Wait at most three minutes for a valid callback, then release the listener.

                Returns:
                    None: The callback port is closed on success, timeout, or application exit.
                """
                try:
                    while time.monotonic() < deadline and owner.spotify_status != "Connected":
                        server.handle_request()
                finally:
                    server.server_close()
                    if owner.spotify_status != "Connected":
                        owner.spotify_status = "Connection expired; try again"

            self._oauth_thread = threading.Thread(target=receive_callback, daemon=True)
            self._oauth_thread.start()
            webbrowser.open(url)
