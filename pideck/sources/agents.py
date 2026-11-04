"""Claude Code agent status.

Claude Code hooks POST small events to /api/agents/event (see hooks/pideck_hook.py).
The registry maps hook events to a display state, tracks one tile per session, and
raises alerts when an agent finishes or needs input.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from pydantic import BaseModel, Field

from ..hub import Hub

# hook event name -> tile state. None = remove the tile.
EVENT_STATE: dict[str, Optional[str]] = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Notification": "waiting",
    "PermissionRequest": "waiting",   # Codex-style approval prompt, if/when the CLI emits it
    "Stop": "done",
    "SessionEnd": None,
}
VALID_STATES = {"idle", "working", "waiting", "done", "error"}
KINDS = {"claude", "codex", "cursor"}
MAX_SESSIONS = 100   # oldest tiles are dropped beyond this


"""# --- Event models ---"""


class UsageWindow(BaseModel):
    """A provider-reported account allowance, expressed as percent used."""

    used_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    window_minutes: int = Field(gt=0, le=525600)
    resets_at: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    label: Optional[str] = Field(None, max_length=24)


class AccountUsage(BaseModel):
    """The short and long allowance windows reported by a connected provider."""

    primary: Optional[UsageWindow] = None
    secondary: Optional[UsageWindow] = None


class AgentEvent(BaseModel):
    session_id: str = Field(max_length=128)
    event: Optional[str] = Field(None, max_length=64)      # Claude Code hook_event_name
    state: Optional[str] = Field(None, max_length=16)      # explicit override (idle/working/waiting/done/error)
    name: Optional[str] = Field(None, max_length=64)       # tile label; defaults to the cwd folder name
    detail: Optional[str] = Field(None, max_length=64)     # extra public label, such as a model slug
    cwd: Optional[str] = Field(None, max_length=512)
    agent: Optional[str] = Field(None, max_length=16)      # which tool: claude (default) or codex
    context_pct: Optional[float] = Field(None, ge=0, le=100)
    usage: Optional[AccountUsage] = None
    message: Optional[str] = Field(None, max_length=500)
    parent_id: Optional[str] = Field(None, max_length=128)  # set when this tile is a subagent


"""# --- Session registry ---"""


class AgentRegistry:
    def __init__(self, hub: Hub, cfg: dict, alert_seconds: float = 8) -> None:
        self.hub = hub
        self.cfg = cfg
        self.alert_seconds = alert_seconds
        self.agents: dict[str, dict] = {}

    async def handle(
            self,
            ev: AgentEvent,
    ) -> dict | None:
        """Apply a lifecycle or usage event and publish any resulting state and alert.

        Args:
            ev: Validated hook event identifying a session and its update.

        Returns:
            The updated session, or None for removed, ignored, or usage-only events.

        Raises:
            ValueError: An explicit state is unknown or the event has no state or type.
        """
        now = time.time()
        if ev.event == "UsageUpdate":
            await self._update_usage(ev, now)
            cur = self.agents.get(ev.session_id)
            if cur is not None and "context_pct" in ev.model_fields_set:
                cur["context_pct"] = ev.context_pct
                await self._publish()
            return cur
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
        kind = ev.agent if ev.agent in KINDS else (cur or {}).get("agent", "claude")
        prev_state = cur["state"] if cur else None
        name = ev.name or (cur or {}).get("name") or _name_from_cwd(ev.cwd) or ev.session_id[:8]
        agent = {
            "id": ev.session_id,
            "name": name,
            "detail": ev.detail or (cur or {}).get("detail"),
            "parent_id": ev.parent_id or (cur or {}).get("parent_id"),
            "agent": kind,
            "cwd": ev.cwd or (cur or {}).get("cwd"),
            "state": new_state,
            "context_pct": ev.context_pct
            if "context_pct" in ev.model_fields_set
            else (cur or {}).get("context_pct"),
            "message": ev.message or ((cur or {}).get("message") if new_state == prev_state else None),
            "updated_at": now,
            "state_since": now if new_state != prev_state else (cur or {}).get("state_since", now),
        }
        self.agents[ev.session_id] = agent
        while len(self.agents) > MAX_SESSIONS:
            del self.agents[min(self.agents, key=lambda k: self.agents[k]["updated_at"])]
        await self._publish()
        await self._update_usage(ev, now)

        if new_state != prev_state and new_state in self.cfg.get("alert_on", []):
            if new_state == "done" and prev_state in (None, "idle"):
                return agent  # a fresh session settling isn't news
            title = f"{name} finished" if new_state == "done" else f"{name} needs you"
            await self.hub.alert(
                "agent", title, agent["message"] or "", state=new_state, agent=kind,
                seconds=self.alert_seconds, session_id=ev.session_id,
            )
        return agent

    async def _update_usage(
            self,
            ev: AgentEvent,
            now: float,
    ) -> None:
        """Publish account usage separately so status-line updates never change tile state.

        Args:
            ev: Incoming hook or status-line event containing optional usage windows.
            now: Server receipt time in Unix seconds.
        """
        if ev.usage is None or ev.agent not in KINDS:
            return
        usage = dict(self.hub.state.get("agent_usage", {}))
        usage[ev.agent] = {**ev.usage.model_dump(), "updated_at": now}
        await self.hub.update("agent_usage", usage)

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
