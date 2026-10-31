"""Core state store + websocket fan-out.

Sources (spotify, slack, agents, mock) never talk to the UI directly. They call
`hub.update(key, value)` for durable state or `hub.alert(...)` for a transient
full-screen takeover; the hub pushes both to every connected browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("pideck.hub")


class Hub:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {"spotify": None, "slack": [], "agents": []}
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await ws.send_text(json.dumps({"type": "snapshot", "state": self.state}))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def _broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:  # client went away mid-send
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def update(self, key: str, value: Any) -> None:
        self.state[key] = value
        await self._broadcast({"type": "state", "key": key, "value": value})

    async def alert(
        self, kind: str, title: str, body: str = "", *, state: str = "", agent: str = "", seconds: float = 8
    ) -> None:
        """Transient takeover. `kind` is 'agent' or 'slack'; `state` styles agent alerts."""
        await self._broadcast(
            {
                "type": "alert",
                "alert": {
                    "id": uuid.uuid4().hex[:8],
                    "kind": kind,
                    "title": title,
                    "body": body,
                    "state": state,
                    "agent": agent,
                    "seconds": seconds,
                    "at": time.time(),
                },
            }
        )
