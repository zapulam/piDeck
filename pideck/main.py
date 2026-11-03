"""FastAPI app: websocket hub + HTTP endpoints + static UI."""
from __future__ import annotations

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

log = logging.getLogger("pideck")
UI_DIR = Path(__file__).parent / "ui"
MAX_WS_CLIENTS = 16
LOOPBACK_IPS = {"127.0.0.1", "::1"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
# The UI is one static page: no inline scripts, styles only via attributes, art from Spotify's CDN.
CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def _host_name(host_header: str) -> str:
    h = host_header.strip().lower()
    return h[: h.index("]") + 1] if h.startswith("[") else h.split(":")[0]


def is_loopback(conn: HTTPConnection) -> bool:
    """True for the kiosk browser on this machine. Both the peer address and the Host header
    must be loopback, so a DNS-rebinding page (Host: evil.example) doesn't qualify."""
    ip = conn.client.host if conn.client else ""
    return ip in LOOPBACK_IPS and _host_name(conn.headers.get("host", "")) in LOOPBACK_HOSTS


def same_origin(conn: HTTPConnection) -> bool:
    """Browsers always send Origin on cross-site requests and websockets; refuse any that isn't us."""
    origin = conn.headers.get("origin")
    return not origin or urlparse(origin).netloc.lower() == conn.headers.get("host", "").lower()


class UiSettingsBody(BaseModel):
    """Pages the bottom bar should show. Unknown ids are ignored."""

    views: list[str]


def create_app(config_path: str | None = None, mock: Optional[bool] = None, lively: bool = True) -> FastAPI:
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

    def authorized(conn: HTTPConnection) -> int:
        """0 = allowed, else the HTTP status to refuse with. Allowed means: from this machine's own
        browser, or carrying the shared token (Authorization: Bearer, or ?token= for browsers)."""
        if not same_origin(conn):
            return 403
        given = conn.headers.get("authorization", "")
        given = given[7:] if given.startswith("Bearer ") else conn.query_params.get("token", "")
        if given:   # a token was presented: it must be right, even from this machine
            return 0 if token and hmac.compare_digest(given.encode(), token.encode()) else 401
        return 0 if is_loopback(conn) else 401

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
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
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="piDeck", lifespan=lifespan)
    app.state.hub, app.state.agents, app.state.feed, app.state.cfg = hub, agents, feed, cfg

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP})
        return resp

    def require_access(conn: HTTPConnection) -> None:
        code = authorized(conn)
        if code:
            raise HTTPException(code, "forbidden origin" if code == 403 else "bad or missing token")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "clients": len(hub.clients)}

    @app.get("/api/config")
    async def config():
        return {"views": load_saved_views(config_file, cfg["views"]), "alert_seconds": secs,
                "mock": cfg["mock"], "rotate": cfg["ui"]["rotate"],
                "agents_stale_hours": mods["agents"].get("stale_hours", 12)}

    @app.post("/api/ui-settings", dependencies=[Depends(require_access)])
    async def update_ui_settings(body: UiSettingsBody):
        return {"views": save_views(config_file, body.views)}

    @app.post("/api/agents/event", dependencies=[Depends(require_access)])
    async def agent_event(ev: AgentEvent):
        try:
            agent = await agents.handle(ev)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "agent": agent}

    @app.delete("/api/agents/{session_id}", dependencies=[Depends(require_access)])
    async def agent_remove(session_id: str):
        agents.agents.pop(session_id, None)
        await agents._publish()
        return {"ok": True}

    @app.post("/api/spotify/{action}", dependencies=[Depends(require_access)])
    async def spotify_control(action: str):
        if action not in ("play", "pause", "next", "previous"):
            raise HTTPException(404)
        if not spotify.configured:
            raise HTTPException(409, "Spotify not configured")
        code = await spotify.control(action)
        if code == 403:
            raise HTTPException(403, "Spotify Premium is required for playback control")
        return {"ok": code < 300, "status": code}

    @app.post("/api/slack/clear", dependencies=[Depends(require_access)])
    async def slack_clear():
        await feed.clear()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
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
    async def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=UI_DIR), name="ui")
    return app
