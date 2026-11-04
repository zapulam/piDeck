"""

FastAPI app: websocket hub + HTTP endpoints + static UI.

"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable
from starlette.requests import Request
from starlette.responses import Response

import asyncio
import contextlib
import hmac
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import HTTPConnection

from .config import load_config, load_saved_views, save_views
from .hub import Hub
from .sources.agents import AgentEvent, AgentRegistry
from .sources.slack import SlackFeed, SlackSource
from .sources.spotify import Spotify

# --- Server configuration ---

log = logging.getLogger("pideck")
UI_DIR = Path(__file__).parent / "ui"
MAX_WS_CLIENTS = 16
LOOPBACK_IPS = {"127.0.0.1", "::1"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
# The UI is one static page: no inline scripts, styles only via attributes, art from Spotify's CDN.
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


# --- Connection authorization helpers ---


def _host_name(
    host_header: str,
    ) -> str:
    """
    Normalize a Host header while preserving a bracketed IPv6 address.

    Args:
        host_header (str): Raw HTTP Host header, optionally including a port.

    Returns:
        str: Lowercase hostname with the port removed and IPv6 brackets preserved.
    """
    h = host_header.strip().lower()
    return h[: h.index("]") + 1] if h.startswith("[") else h.split(":")[0]


def is_loopback(
    conn: HTTPConnection,
    ) -> bool:
    """
    True for the kiosk browser on this machine. Both the peer address and the Host header
    must be loopback, so a DNS-rebinding page (Host: evil.example) doesn't qualify.

    Args:
        conn (HTTPConnection): Incoming HTTP or websocket connection whose origin and access are
            checked.

    Returns:
        bool: True only when both the peer address and requested hostname identify this machine.
    """
    ip = conn.client.host if conn.client else ""
    return ip in LOOPBACK_IPS and _host_name(conn.headers.get("host", "")) in LOOPBACK_HOSTS


def same_origin(
    conn: HTTPConnection,
    ) -> bool:
    """
    Browsers always send Origin on cross-site requests and websockets; refuse any that isn't us.

    Args:
        conn (HTTPConnection): Incoming HTTP or websocket connection whose origin and access are
            checked.

    Returns:
        bool: True when Origin is absent or matches the request Host header.
    """
    origin = conn.headers.get("origin")
    return not origin or urlparse(origin).netloc.lower() == conn.headers.get("host", "").lower()


# --- Application and request models ---


class UiSettingsBody(BaseModel):
    """Pages the bottom bar should show. Unknown ids are ignored."""

    views: list[str]


def create_app(
    config_path: str | None = None,
    mock: Optional[bool] = None,
    lively: bool = True,
    prime: bool = False,
    ) -> FastAPI:
    """
    Create the application, source lifecycles, authenticated routes, and static UI.

    Args:
        config_path (str | None): Application configuration path, or None to use the default
            location.
        mock (Optional[bool]): Whether generated example data replaces live provider connections.
        lively (bool): Whether the demo should continue emitting random changes after startup.
        prime (bool): Whether existing local sessions are discovered when the server starts.

    Returns:
        FastAPI: Configured FastAPI application with source state, lifecycle tasks, routes, and UI
            assets.
    """
    config_file = Path(config_path) if config_path else Path("config.yaml")
    cfg = load_config(config_path)
    if mock is not None:
        cfg["mock"] = mock
    mods = cfg["modules"]
    secs = float(cfg["alerts"]["seconds"])

    hub = Hub()
    agents = AgentRegistry(hub, mods["agents"], secs)
    feed = SlackFeed(hub, mods["slack"], secs)
    spotify = Spotify(hub, mods["spotify"])
    token = os.environ.get("PIDECK_TOKEN", "")

    # --- Connection access checks ---

    def authorized(
        conn: HTTPConnection,
        ) -> int:
        """
        0 = allowed, else the HTTP status to refuse with. Allowed means: from this machine's own
        browser, or carrying the shared token (Authorization: Bearer, or ?token= for browsers).

        Args:
            conn (HTTPConnection): Incoming HTTP or websocket connection whose origin and access are
                checked.

        Returns:
            int: Zero for an allowed connection, 401 for missing/invalid authentication, or 403 for
                a foreign origin.
        """
        if not same_origin(conn):
            return 403
        given = conn.headers.get("authorization", "")
        given = given[7:] if given.startswith("Bearer ") else conn.query_params.get("token", "")
        if given:   # a token was presented: it must be right, even from this machine
            return 0 if token and hmac.compare_digest(given.encode(), token.encode()) else 401
        return 0 if is_loopback(conn) else 401

    # --- Background source lifecycle ---

    @contextlib.asynccontextmanager
    async def lifespan(
        app: FastAPI,
        ) -> AsyncIterator[None]:
        """
        Start configured sources and stop their background tasks when the app closes.

        Args:
            app (FastAPI): Application whose background sources are started and stopped.

        Returns:
            AsyncIterator[None]: Async context generator that keeps source tasks alive until
                application shutdown.
        """
        tasks: list[asyncio.Task] = []
        if cfg["server"]["host"] not in ("127.0.0.1", "localhost") and not token:
            log.warning("No PIDECK_TOKEN set: only this machine can use piDeck. Set one to accept hooks from other computers.")
        if cfg["mock"]:
            from .sources import mock as mock_src

            tasks.append(asyncio.create_task(mock_src.run(hub, agents, feed, lively)))
        else:
            if mods["spotify"]["enabled"]:
                tasks.append(asyncio.create_task(spotify.run()))
            if mods["slack"]["enabled"]:
                tasks.append(asyncio.create_task(SlackSource(feed, mods["slack"]).run()))
        if mods["agents"]["enabled"]:
            tasks.append(asyncio.create_task(agents.prune_forever()))
            if prime:
                from .local_prime import launch_prime

                async def _refresh_prime() -> None:
                    """
                    Reload local sessions and allowance bars until the server stops.

                    Returns:
                        None: No value is returned.
                    """
                    while True:
                        await asyncio.to_thread(launch_prime)
                        await asyncio.sleep(900)

                tasks.append(asyncio.create_task(_refresh_prime()))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="piDeck", lifespan=lifespan)
    app.state.hub, app.state.agents, app.state.feed, app.state.cfg = hub, agents, feed, cfg

    # --- HTTP security and display configuration ---

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
        """
        Attach browser security headers to every HTTP response.

        Args:
            request (Request): Incoming HTTP request passed to the next middleware handler.
            call_next (Callable[[Request], Awaitable[Response]]): Next middleware or route handler
                in the HTTP processing chain.

        Returns:
            Response: Downstream response augmented with content, framing, referrer, and script
                security headers.
        """
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP})
        return resp

    def require_access(
        conn: HTTPConnection,
        ) -> None:
        """
        Require an authorized local or token-authenticated connection.

        Args:
            conn (HTTPConnection): Incoming HTTP or websocket connection whose origin and access are
                checked.

        Returns:
            None: No value is returned.
        """
        code = authorized(conn)
        if code:
            raise HTTPException(code, "forbidden origin" if code == 403 else "bad or missing token")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """
        Report server availability and the number of connected displays.

        Returns:
            dict[str, Any]: Health status and the number of currently connected display clients.
        """
        return {"ok": True, "clients": len(hub.clients)}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        """
        Return public display configuration and saved page choices.

        Returns:
            dict[str, Any]: Public page selection, alert duration, rotation, mock mode, and agent
                staleness settings.
        """
        return {"views": load_saved_views(config_file, cfg["views"]), "alert_seconds": secs,
                "mock": cfg["mock"], "rotate": cfg["ui"]["rotate"],
                "agents_stale_hours": mods["agents"].get("stale_hours", 12)}

    @app.post("/api/ui-settings", dependencies=[Depends(require_access)])
    async def update_ui_settings(
        body: UiSettingsBody,
        ) -> dict[str, Any]:
        """
        Save the selected display pages and return the normalized selection.

        Args:
            body (UiSettingsBody): Validated request body or optional JSON fields to send.

        Returns:
            dict[str, Any]: Normalized saved page IDs under the views key.
        """
        return {"views": save_views(config_file, body.views)}

    # --- Agent and integration controls ---

    @app.post("/api/agents/event", dependencies=[Depends(require_access)])
    async def agent_event(
        ev: AgentEvent,
        ) -> dict[str, Any]:
        """
        Apply a validated agent event and return its updated session state.

        Args:
            ev (AgentEvent): Agent event containing the session identifier and permitted update
                fields.

        Returns:
            dict[str, Any]: Success flag and updated session, or None for a removed or ignored
                session.
        """
        try:
            agent = await agents.handle(ev)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "agent": agent}

    @app.delete("/api/agents/{session_id}", dependencies=[Depends(require_access)])
    async def agent_remove(
        session_id: str,
        ) -> dict[str, Any]:
        """
        Remove a session tile and notify connected displays.

        Args:
            session_id (str): Identifier of the session being updated or removed.

        Returns:
            dict[str, Any]: Success flag after the session removal has been broadcast.
        """
        agents.agents.pop(session_id, None)
        await agents._publish()
        return {"ok": True}

    @app.post("/api/spotify/{action}", dependencies=[Depends(require_access)])
    async def spotify_control(
        action: str,
        ) -> dict[str, Any]:
        """
        Run a supported Spotify playback action when the integration is configured.

        Args:
            action (str): Requested playback action, such as play, pause, next, or previous.

        Returns:
            dict[str, Any]: Provider HTTP status and whether the playback request succeeded.
        """
        if action not in ("play", "pause", "next", "previous"):
            raise HTTPException(404)
        if not spotify.configured:
            raise HTTPException(409, "Spotify not configured")
        code = await spotify.control(action)
        if code == 403:
            raise HTTPException(403, "Spotify Premium is required for playback control")
        return {"ok": code < 300, "status": code}

    @app.post("/api/slack/clear", dependencies=[Depends(require_access)])
    async def slack_clear() -> dict[str, Any]:
        """
        Clear the recent Slack feed and notify connected displays.

        Returns:
            dict[str, Any]: Success flag after the cleared feed has been broadcast.
        """
        await feed.clear()
        return {"ok": True}

    # --- Live display connection and entry page ---

    @app.websocket("/ws")
    async def ws(
        ws: WebSocket,
        ) -> None:
        """
        Authorize a display websocket and keep it registered until it disconnects.

        Args:
            ws (WebSocket): Display websocket to accept, update, or remove.

        Returns:
            None: No value is returned.
        """
        if authorized(ws):
            return await ws.close(code=1008)      # policy violation: not us, or no token
        if len(hub.clients) >= MAX_WS_CLIENTS:
            return await ws.close(code=1013)      # try again later
        await hub.connect(ws)
        try:
            while True:
                await ws.receive_text()  # we only push; this detects disconnects
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(ws)

    @app.get("/")
    async def index() -> FileResponse:
        """
        Serve the application entry page without caching its HTML.

        Returns:
            FileResponse: The application HTML entry page with browser caching disabled.
        """
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=UI_DIR), name="ui")
    return app
