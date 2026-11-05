"""

Core state store + websocket fan-out.

Sources (spotify, slack, agents, mock) never talk to the UI directly. They call
`hub.update(key, value)` for durable state or `hub.alert(...)` for a transient
full-screen takeover; the hub pushes both to every connected browser.

"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import Any

from fastapi import WebSocket

# --- Shared state and display broadcasts ---

log = logging.getLogger("pideck.hub")


class Hub:
    def __init__(
        self,
        ) -> None:
        """
        Initialize the shared source state and connected display registry.

        Returns:
            None: No value is returned.
        """
        self.state: dict[str, Any] = {"spotify": None, "slack": [], "agents": [], "agent_usage": {}}
        self.clients: set[WebSocket] = set()
        self._connect_lock = asyncio.Lock()
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(
        self,
        ws: WebSocket,
        ) -> bool:
        """
        Accept a display websocket and send its initial state snapshot.

        Args:
            ws (WebSocket): Display websocket to accept, update, or remove.

        Returns:
            bool: True for a registered display, or False when the limit or initial send fails.
        """
        async with self._connect_lock:
            if len(self.clients) >= 16:
                await ws.close(code=1013)
                return False
            self.clients.add(ws)
            self._send_locks[ws] = asyncio.Lock()
        try:
            async with asyncio.timeout(2):
                await ws.accept(subprotocol="pideck-v1" if "pideck-v1" in ws.scope.get("subprotocols", []) else None)
            return await self._send(ws, json.dumps({"type": "snapshot", "state": self.state}))
        except Exception:
            self.disconnect(ws)
            return False

    def disconnect(
        self,
        ws: WebSocket,
        ) -> None:
        """
        Remove a display websocket from the connected-client registry.

        Args:
            ws (WebSocket): Display websocket to accept, update, or remove.

        Returns:
            None: No value is returned.
        """
        self.clients.discard(ws)
        self._send_locks.pop(ws, None)

    async def _send(
        self,
        ws: WebSocket,
        text: str,
        ) -> bool:
        """
        Serialize one display's sends and disconnect it when backpressure exceeds two seconds.

        Args:
            ws (WebSocket): Display receiving this event.
            text (str): Serialized event to send.

        Returns:
            bool: True after delivery, or False after the slow or closed client is removed.
        """
        try:
            async with asyncio.timeout(2):
                async with self._send_locks.setdefault(ws, asyncio.Lock()):
                    await ws.send_text(text)
            return True
        except Exception:
            self.disconnect(ws)
            with contextlib.suppress(Exception):
                async with asyncio.timeout(0.1):
                    await ws.close(code=1013)
            return False

    async def _broadcast(
        self,
        msg: dict,
        ) -> None:
        """
        Send a JSON event to each display and discard disconnected clients.

        Args:
            msg (dict): JSON-compatible event to deliver to connected displays.

        Returns:
            None: No value is returned.
        """
        text = json.dumps(msg)
        await asyncio.gather(*(self._send(ws, text) for ws in list(self.clients)))

    async def update(
        self,
        key: str,
        value: Any,
        ) -> None:
        """
        Store a source state value and broadcast the update to connected displays.

        Args:
            key (str): State key or stored field to look up or update.
            value (Any): Value to normalize, validate, or publish.

        Returns:
            None: No value is returned.
        """
        self.state[key] = value
        await self._broadcast({"type": "state", "key": key, "value": value})

    async def alert(
        self,
        kind: str,
        title: str,
        body: str = '',
        *,
        state: str = '',
        agent: str = '',
        seconds: float = 8,
        session_id: str = '',
        ) -> None:
        """
        Broadcast a transient takeover to connected displays.

        Args:
            kind (str): Alert source, either agent or Slack.
            title (str): Main text shown in the takeover.
            body (str): Optional supporting message.
            state (str): Agent state used to style the takeover.
            agent (str): Provider associated with an agent alert.
            seconds (float): Requested display duration in seconds.
            session_id (str): Exact session owning an agent alert, even for duplicate names.

        Returns:
            None: No value is returned.
        """
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
                    "session_id": session_id,
                    "seconds": seconds,
                    "at": time.time(),
                },
            }
        )
