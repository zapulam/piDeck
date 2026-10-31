"""Claude Code agent status.

Claude Code hooks POST small events to /api/agents/event (see hooks/pideck_hook.py).
The registry maps hook events to a display state, tracks one tile per session, and
raises alerts when an agent finishes or needs input.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from pydantic import BaseModel

from ..hub import Hub

# hook event name -> tile state. None = remove the tile.
EVENT_STATE: dict[str, Optional[str]] = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Notification": "waiting",
    "Stop": "done",
    "SessionEnd": None,
}
VALID_STATES = {"idle", "working", "waiting", "done", "error"}


class AgentEvent(BaseModel):
    session_id: str
    event: Optional[str] = None      # Claude Code hook_event_name
    state: Optional[str] = None      # explicit override (idle/working/waiting/done/error)
    name: Optional[str] = None       # tile label; defaults to the cwd folder name
    cwd: Optional[str] = None
    context_pct: Optional[float] = None
    message: Optional[str] = None


class AgentRegistry:
    def __init__(self, hub: Hub, cfg: dict, alert_seconds: float = 8) -> None:
        self.hub = hub
        self.cfg = cfg
        self.alert_seconds = alert_seconds
        self.agents: dict[str, dict] = {}

    async def handle(self, ev: AgentEvent) -> dict | None:
        now = time.time()
        if ev.state is not None:
            if ev.state not in VALID_STATES:
                raise ValueError(f"unknown state {ev.state!r}")
            new_state: Optional[str] = ev.state
        elif ev.event is not None:
            if ev.event not in EVENT_STATE:
                return None  # an event we deliberately ignore
            new_state = EVENT_STATE[ev.event]
        else:
            raise ValueError("need `event` or `state`")

        if new_state is None:
            self.agents.pop(ev.session_id, None)
            await self._publish()
            return None

        cur = self.agents.get(ev.session_id)
        prev_state = cur["state"] if cur else None
        name = ev.name or (cur or {}).get("name") or _name_from_cwd(ev.cwd) or ev.session_id[:8]
        agent = {
            "id": ev.session_id,
            "name": name,
            "cwd": ev.cwd or (cur or {}).get("cwd"),
            "state": new_state,
            "context_pct": ev.context_pct
            if ev.context_pct is not None
            else (cur or {}).get("context_pct"),
            "message": ev.message or ((cur or {}).get("message") if new_state == prev_state else None),
            "updated_at": now,
            "state_since": now if new_state != prev_state else (cur or {}).get("state_since", now),
        }
        self.agents[ev.session_id] = agent
        await self._publish()

        if new_state != prev_state and new_state in self.cfg.get("alert_on", []):
            if new_state == "done" and prev_state in (None, "idle"):
                return agent  # a fresh session settling isn't news
            title = f"{name} finished" if new_state == "done" else f"{name} needs you"
            await self.hub.alert(
                "agent", title, agent["message"] or "", state=new_state, seconds=self.alert_seconds
            )
        return agent

    async def _publish(self) -> None:
        await self.hub.update("agents", list(self.agents.values()))

    async def prune_forever(self) -> None:
        stale = float(self.cfg.get("stale_hours", 12)) * 3600
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - stale
            gone = [k for k, a in self.agents.items() if a["updated_at"] < cutoff]
            for k in gone:
                del self.agents[k]
            if gone:
                await self._publish()


def _name_from_cwd(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    return cwd.replace("\\", "/").rstrip("/").split("/")[-1] or None
