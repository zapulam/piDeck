"""

Claude Code agent status.

Claude Code hooks POST small events to /api/agents/event (see hooks/pideck_hook.py).
The registry maps hook events to a display state, tracks one tile per session, and
raises alerts when an agent finishes or needs input.

"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import math
from pathlib import Path
import time
from typing import Optional

from pydantic import BaseModel, Field

from ..hub import Hub
from hooks.pideck_hook import private_write

# hook event name -> tile state. None = remove the tile.
EVENT_STATE: dict[str, Optional[str]] = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PostToolUseFailure": "working",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
    "Stop": "done",
    "Interrupt": "error",
    "StopFailure": "error",
    "SessionEnd": None,
    "BackgroundUpdate": "working",
}
VALID_STATES = {"idle", "working", "waiting", "done", "error", "unknown"}
KINDS = {"claude", "codex", "cursor"}
MAX_SESSIONS = 100   # oldest tiles are dropped beyond this


# --- Event models ---


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
    name: Optional[str] = Field(None, max_length=64)       # tile label; summary/title preferred over id hash
    detail: Optional[str] = Field(None, max_length=64)     # extra public label, such as a model slug
    cwd: Optional[str] = Field(None, max_length=512)
    agent: Optional[str] = Field(None, max_length=16)      # which tool: claude (default) or codex
    context_pct: Optional[float] = Field(None, ge=0, le=100)
    usage: Optional[AccountUsage] = None
    note: Optional[str] = Field(None, max_length=40)       # why account usage is unavailable
    message: Optional[str] = Field(None, max_length=500)
    parent_id: Optional[str] = Field(None, max_length=128)  # set when this tile is a subagent
    provider_session_id: Optional[str] = Field(None, max_length=128)
    # Provider observation time, retained across offline replay.
    activity_at: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    event_id: Optional[str] = Field(None, max_length=64)
    tool_use_id: Optional[str] = Field(None, max_length=128)
    tool_name: Optional[str] = Field(None, max_length=64)
    turn_id: Optional[str] = Field(None, max_length=128)
    background_tasks: Optional[list[str]] = Field(None, max_length=64)
    replay: bool = False


# --- Session registry ---


class AgentRegistry:
    def __init__(
        self,
        hub: Hub,
        cfg: dict,
        alert_seconds: float = 8,
        state_path: Path | None = None,
        ) -> None:
        """
        Initialize session tracking and agent alert configuration.

        Args:
            hub (Hub): Shared hub used to publish source state and display alerts.
            cfg (dict): Configuration options for this integration.
            alert_seconds (float): Duration in seconds for a full-screen attention or completion
                alert.
            state_path (Path | None): Optional private file retaining manual dismissal cutoffs.

        Returns:
            None: No value is returned.
        """
        self.hub = hub
        self.cfg = cfg
        self.alert_seconds = alert_seconds
        self.agents: dict[str, dict] = {}
        self.tools: dict[str, dict[str, str]] = {}
        self.jobs: dict[str, set[str]] = {}
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.state_path = state_path
        try:
            self.dismissed = json.loads(state_path.read_text(encoding="utf-8")) if state_path else {}
        except (OSError, ValueError):
            self.dismissed = {}
        if not isinstance(self.dismissed, dict):
            self.dismissed = {}
        self.dismissed = {key: value for key, value in self.dismissed.items()
                          if isinstance(key, str) and isinstance(value, (int, float)) and math.isfinite(value)}

    async def handle(
        self,
        ev: AgentEvent,
        ) -> dict | None:
        """
        Apply a lifecycle or usage event and publish any resulting state and alert.

        Args:
            ev (AgentEvent): Validated hook event identifying a session and its update.

        Returns:
            dict | None: The updated session, or None for removed, ignored, or usage-only events.

        Raises:
            ValueError: An explicit state is unknown or the event has no state or type.
        """
        now = time.time()
        cur = self.agents.get(ev.session_id)
        if ev.event_id:
            if ev.event_id in self.seen:
                return cur
            self.seen[ev.event_id] = None
            while len(self.seen) > 20000:
                self.seen.popitem(last=False)
        observed = ev.activity_at if ev.activity_at is not None else now
        if ev.event == "UsageUpdate":
            await self._update_usage(ev, observed)
            if cur is not None and "context_pct" in ev.model_fields_set and observed >= cur.get("context_at", 0):
                cur["context_pct"] = ev.context_pct
                cur["context_at"] = observed
                await self._publish()
            return cur
        if observed <= self.dismissed.get(ev.session_id, 0):
            return None
        measurement = ev.event == "BackgroundUpdate"
        if cur is not None and observed < cur["activity_at"] and not measurement:
            return cur
        if ev.state is not None and ev.state not in VALID_STATES:
            raise ValueError(f"unknown state {ev.state!r}")
        if ev.state is None and ev.event not in EVENT_STATE:
            if ev.event is None:
                raise ValueError("need `event` or `state`")
            return None
        if ev.event == "SessionStart":
            return cur  # Opening a harness is not task activity or completion.
        if ev.event == "SessionEnd":
            # Cursor closes a view while its tools can still be running.
            # A close is neither a completion nor an acknowledgment of the result.
            if cur is not None:
                cur["closed"] = True
                if cur["state"] in {"working", "waiting"} and not self.jobs.get(ev.session_id) and not self.tools.get(ev.session_id):
                    cur["state"] = "unknown"
                    cur["activity_label"] = "Status unknown"
                await self._publish()
            return cur
        if cur is None and measurement:
            return None
        tools = self.tools.setdefault(ev.session_id, {})
        jobs = self.jobs.setdefault(ev.session_id, set())
        if ev.event == "UserPromptSubmit":
            tools.clear()
        if ev.background_tasks is not None:
            jobs.clear()
            jobs.update(ev.background_tasks)
        if ev.tool_use_id:
            if ev.event == "PreToolUse":
                tools[ev.tool_use_id] = ev.tool_name or ""
            elif ev.event in {"PostToolUse", "PostToolUseFailure"}:
                tools.pop(ev.tool_use_id, None)
        ended = (cur or {}).get("turn_ended", False)
        if ev.event in {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure"}:
            ended = False
        elif ev.event in {"Stop", "StopFailure", "Interrupt"}:
            ended = True
            tools.clear()
        state = ev.state or EVENT_STATE[ev.event]
        if measurement:
            state = "done" if ended and not jobs and not tools and cur["state"] == "working" else cur["state"]
        if jobs and ended and state in {"done", "error"}:
            state = "working"
        prev_state = (cur or {}).get("state")
        name = ev.name or (cur or {}).get("name") or _name_from_cwd(ev.cwd) or ev.session_id[:8]
        kind = ev.agent if ev.agent in KINDS else (cur or {}).get("agent", "claude")
        names = {name.lower() for name in tools.values()}
        label = "Working"
        if tools:
            if names & {"bash", "shell", "powershell", "exec_command"}:
                label = "Running command"
            elif names & {"task", "agent", "collaborationspawn_agent", "spawn_agent"}:
                label = "Running subagents"
            elif names & {"collaborationwait_agent", "wait_agent"}:
                label = "Waiting for subagents"
            else:
                label = "Using tool"
        elif jobs:
            label = "Background work"
        if state == "waiting":
            label = "Needs you"
        elif state == "done":
            label = "Done"
        elif state == "error":
            label = "Stopped" if ev.event == "Interrupt" else "Error"
        elif state == "idle":
            label = "Idle"
        elif state == "unknown":
            label = "Status unknown"
        activity = cur["activity_at"] if measurement else observed
        agent = {
            "id": ev.session_id, "name": name, "agent": kind, "state": state,
            "detail": ev.detail or (cur or {}).get("detail"),
            "parent_id": ev.parent_id or (cur or {}).get("parent_id"),
            "provider_session_id": ev.provider_session_id or (cur or {}).get("provider_session_id"),
            "cwd": ev.cwd or (cur or {}).get("cwd"),
            "context_pct": ev.context_pct if "context_pct" in ev.model_fields_set else (cur or {}).get("context_pct"),
            "message": ev.message, "updated_at": observed, "activity_at": activity,
            "state_since": observed if state != prev_state else cur["state_since"],
            "turn_id": ev.turn_id or (cur or {}).get("turn_id"),
            "turn_ended": ended, "activity_label": label,
            "running_tool_count": len(tools), "background_count": len(jobs),
            "active_tools": sorted(set(name for name in tools.values() if name)),
            "restored": ev.replay if not measurement else cur.get("restored", False),
            "context_at": observed if "context_pct" in ev.model_fields_set else (cur or {}).get("context_at", 0),
        }
        self.agents[ev.session_id] = agent
        if state == "done":
            for child in self.agents.values():
                if child.get("parent_id") == ev.session_id and child["state"] == "working" and not self.tools.get(child["id"]) and not self.jobs.get(child["id"]):
                    child["state"] = "unknown"
                    child["activity_label"] = "Status unknown"
        while len(self.agents) > MAX_SESSIONS:
            oldest = min(self.agents, key=lambda key: self.agents[key]["activity_at"])
            self.agents.pop(oldest)
            self.tools.pop(oldest, None)
            self.jobs.pop(oldest, None)
        await self._publish()
        await self._update_usage(ev, observed)
        if not ev.replay and (state != "done" or not agent["parent_id"]) and state != prev_state and state in self.cfg.get("alert_on", []):
            if state == "done" and prev_state in (None, "idle"):
                return agent
            title = f"{name} finished" if state == "done" else f"{name} needs you"
            await self.hub.alert("agent", title, ev.message or "", state=state, agent=kind,
                                 seconds=self.alert_seconds, session_id=ev.session_id)
        return agent

    async def finish_restore(
        self,
        ) -> None:
        """
        Mark replayed in-flight work unknown until a fresh provider hook confirms its state.

        Returns:
            None: Old observations remain visible and dismissible without claiming live activity.
        """
        changed = False
        for agent in self.agents.values():
            if agent.get("restored") and agent["state"] in {"working", "waiting"}:
                agent["state"] = "unknown"
                agent["activity_label"] = "Status unknown"
                changed = True
        if changed:
            await self._publish()

    async def dismiss(
        self,
        session_id: str,
        only_if_finished: bool = False,
        ) -> None:
        """
        Hide a card and its descendants without stopping work or allowing older replay to restore them.

        Args:
            session_id (str): Root of the family dismissed by the person or a verified read receipt.
            only_if_finished (bool): Keep a read-acknowledged family visible while any worker
                still has active or uncertain work. Manual dismissal leaves this False.

        Returns:
            None: The local card is removed; later real provider activity can recreate it.
        """
        hidden = {session_id}
        while True:
            descendants = {sid for sid, agent in self.agents.items() if agent.get("parent_id") in hidden}
            if descendants <= hidden:
                break
            hidden.update(descendants)
        if only_if_finished and any(self.agents[sid]["state"] not in {"done", "idle", "error"}
                                    for sid in hidden if sid in self.agents):
            return
        for sid in hidden:
            self.dismissed[sid] = time.time()
            self.agents.pop(sid, None)
            self.tools.pop(sid, None)
            self.jobs.pop(sid, None)
        self.dismissed = dict(sorted(self.dismissed.items(), key=lambda pair: pair[1])[-1000:])
        if self.state_path is not None:
            private_write(self.state_path, json.dumps(self.dismissed) + "\n")
        await self._publish()

    async def _update_usage(
        self,
        ev: AgentEvent,
        now: float,
        ) -> None:
        """
        Publish account usage separately so status-line updates never change tile state.

        Args:
            ev (AgentEvent): Incoming hook or status-line event containing optional usage windows
                and/or a short note explaining why bars are unavailable.
            now (float): Server receipt time in Unix seconds.

        Returns:
            None: No value is returned.
        """
        if ev.agent not in KINDS:
            return
        if ev.usage is None and not ev.note:
            return
        usage = dict(self.hub.state.get("agent_usage", {}))
        entry = dict(usage.get(ev.agent) or {})
        if now < entry.get("updated_at", 0):
            return
        if ev.note in {"unsupported", "codex_unavailable"}:
            entry = {"primary": None, "secondary": None, "updated_at": now}
        if ev.usage is not None:
            entry = {**ev.usage.model_dump(), "updated_at": now}
        if ev.note:
            entry["note"] = ev.note
        elif "note" in entry and ev.usage is not None:
            entry.pop("note", None)
        usage[ev.agent] = entry
        await self.hub.update("agent_usage", usage)

    async def _publish(
        self,
        ) -> None:
        """
        Broadcast the current session registry to all connected displays.

        Returns:
            None: No value is returned.
        """
        await self.hub.update("agents", list(self.agents.values()))

    async def prune_forever(
        self,
        ) -> None:
        """
        Periodically remove inactive sessions whose last real activity is older than stale_hours.

        Real work is never declared finished by an elapsed-time guess. Inactive results
        expire after stale_hours (24 by default), or earlier through explicit dismissal.

        Returns:
            None: No value is returned.
        """
        stale = float(self.cfg.get("stale_hours", 24)) * 3600
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - stale
            gone = [
                k for k, a in self.agents.items()
                if a["state"] not in {"working", "waiting"}
                and float(a.get("activity_at") or a["updated_at"]) < cutoff
            ]
            for k in gone:
                del self.agents[k]
                self.tools.pop(k, None)
                self.jobs.pop(k, None)
            if gone:
                await self._publish()


def _name_from_cwd(
    cwd: Optional[str],
    ) -> Optional[str]:
    """
    Extract a project folder name from a POSIX or Windows working directory.

    Args:
        cwd (Optional[str]): Session working directory, or None when the hook did not provide one.

    Returns:
        Optional[str]: Final directory component, or None for an absent or empty working directory.
    """
    if not cwd:
        return None
    return cwd.replace("\\", "/").rstrip("/").split("/")[-1] or None
