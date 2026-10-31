"""FastAPI app: websocket hub + HTTP endpoints + static UI."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .hub import Hub
from .sources.agents import AgentEvent, AgentRegistry
from .sources.slack import SlackFeed, SlackSource
from .sources.spotify import Spotify

log = logging.getLogger("pideck")
UI_DIR = Path(__file__).parent / "ui"


def create_app(config_path: str | None = None, mock: Optional[bool] = None, lively: bool = True) -> FastAPI:
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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if cfg["server"]["host"] not in ("127.0.0.1", "localhost") and not token and not cfg["mock"]:
            log.warning("Listening on the network with no PIDECK_TOKEN: anyone on your LAN can post agent events.")
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

    def require_token(authorization: Optional[str] = Header(None)) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(401, "bad or missing token")

    @app.get("/api/health")
    async def health():
        return {"ok": True, "clients": len(hub.clients)}

    @app.get("/api/config")
    async def config():
        return {"views": cfg["views"], "alert_seconds": secs, "mock": cfg["mock"], "rotate": cfg["ui"]["rotate"]}

    @app.post("/api/agents/event", dependencies=[Depends(require_token)])
    async def agent_event(ev: AgentEvent):
        try:
            agent = await agents.handle(ev)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "agent": agent}

    @app.delete("/api/agents/{session_id}")
    async def agent_remove(session_id: str):
        agents.agents.pop(session_id, None)
        await agents._publish()
        return {"ok": True}

    @app.post("/api/spotify/{action}")
    async def spotify_control(action: str):
        if action not in ("play", "pause", "next", "previous"):
            raise HTTPException(404)
        if not spotify.configured:
            raise HTTPException(409, "Spotify not configured")
        code = await spotify.control(action)
        if code == 403:
            raise HTTPException(403, "Spotify Premium is required for playback control")
        return {"ok": code < 300, "status": code}

    @app.post("/api/slack/clear")
    async def slack_clear():
        await feed.clear()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
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
