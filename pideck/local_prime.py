"""

Replay real hook events and refresh verified measurements without transcript-age guesses.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from hooks.event_store import destination, pending
from hooks.install import MARK, _is_ours, main as install_hooks, read_json
from hooks.runtime import codex_viewed, cursor_metrics
from .codex_usage import allowances
from .sources.agents import AgentEvent, AgentRegistry

log = logging.getLogger(__name__)


# --- Owned local connection repair ---


def synchronize_hooks(
    url: str,
    ) -> bool:
    """
    Refresh an existing owned loopback integration after token or helper upgrades.

    Args:
        url (str): Address of the native server owning this process's hook credential.

    Returns:
        bool: True for refreshed existing integrations; False for unconfigured or intentionally
            remote destinations, which are never overwritten automatically.
    """
    home = Path.home()
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    saved = home / ".config/pideck/hook.json"
    if not saved.is_file():
        return False
    try:
        config = read_json(saved)
        if destination(config.get("url", "")) != destination(url):
            return False
        flags = []
        for provider, relative in (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json"),
                                   ("cursor", ".cursor/hooks.json")):
            settings = read_json(home / relative)
            if MARK in str(settings.get("hooks", {})) or _is_ours(settings.get("statusLine") or {}):
                flags.append("--" + provider)
        if not flags:
            return False
        return install_hooks(["--url", url, "--token", os.environ["PIDECK_HOOK_TOKEN"], "--no-check", *flags]) == 0
    except (OSError, ValueError, KeyError):
        log.warning("Local agent connection needs reconnecting in Settings")
        return False


# --- Offline replay and local runtime measurements ---


async def monitor(
    agents: AgentRegistry,
    address: Callable[[], str],
    ) -> None:
    """
    Consume durable hook events, reported context/jobs, account limits, and exact read receipts.

    Args:
        agents (AgentRegistry): Registry receiving actual provider updates.
        address (Callable[[], str]): Bound server address, including a dynamic port.

    Returns:
        None: Runs until application shutdown cancels this source.
    """
    await asyncio.to_thread(synchronize_hooks, address())
    sequence = 0
    restoring = True
    cursor_values = {}
    if (Path.home() / ".cursor").is_dir():
        await agents.handle(AgentEvent(session_id="usage:cursor", event="UsageUpdate", agent="cursor", note="unsupported"))
    usage_task = asyncio.create_task(refresh_allowances(agents))
    try:
        while True:
            rows = await asyncio.to_thread(pending, address(), sequence, os.environ)
            for sequence, body in rows:
                try:
                    await agents.handle(AgentEvent(**{**body, "replay": restoring}))
                except ValueError:
                    log.warning("Ignored an unsupported cached agent event")
            if len(rows) < 2000:
                if restoring:
                    await agents.finish_restore()
                restoring = False
            cursor_values = {sid: value for sid, value in cursor_values.items() if sid in agents.agents}
            for current in list(agents.agents.values()):
                sid = current["id"]
                if current["agent"] == "cursor" and current["state"] in {"working", "waiting"}:
                    metrics = await asyncio.to_thread(cursor_metrics, sid.removeprefix("cursor:"), os.environ)
                    if metrics and metrics != cursor_values.get(sid):
                        cursor_values[sid] = metrics
                        if "context_pct" in metrics:
                            await agents.handle(AgentEvent(session_id=sid, agent="cursor", event="UsageUpdate",
                                                           context_pct=metrics["context_pct"]))
                        if "background_tasks" in metrics and current["state"] == "working":
                            await agents.handle(AgentEvent(session_id=sid, agent="cursor", event="BackgroundUpdate",
                                                           background_tasks=metrics["background_tasks"]))
                elif current["agent"] == "codex" and current["state"] == "done":
                    if await asyncio.to_thread(codex_viewed, sid, current.get("turn_id") or "", Path.home()):
                        await agents.dismiss(sid)
            await asyncio.sleep(1)
    finally:
        usage_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await usage_task


async def refresh_allowances(
    agents: AgentRegistry,
    ) -> None:
    """
    Refresh account allowances independently so a slow provider never delays lifecycle replay.

    Args:
        agents (AgentRegistry): Registry receiving fresh account readings and unavailable states.

    Returns:
        None: Runs until the local monitor cancels its owned task at shutdown.
    """
    while True:
        if (Path.home() / ".codex").is_dir():
            usage = await allowances(os.environ)
            await agents.handle(AgentEvent(session_id="usage:codex", event="UsageUpdate", agent="codex",
                                           usage=usage, note=None if usage else "codex_unavailable"))
        await asyncio.sleep(60)
