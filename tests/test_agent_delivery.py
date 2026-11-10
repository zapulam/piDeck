"""

Regress real lifecycle boundaries, offline replay, authentication repair, and manual dismissal.

"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
import pytest

from hooks.event_store import pending, remember
from hooks.runtime import codex_viewed, cursor_metrics
from pideck.hub import Hub
from pideck.local_prime import synchronize_hooks
from pideck.sources.agents import AgentEvent, AgentRegistry


# --- Provider lifecycle boundaries ---


def test_response_end_waits_for_background_and_close_is_not_completion(
    client: TestClient,
    ) -> None:
    """
    Keep real background work active across a response end and a closed view.

    Args:
        client (TestClient): Isolated authenticated application client.

    Returns:
        None: Background completion, not view closure, produces the terminal state.
    """
    base = {"session_id": "c", "agent": "claude"}
    client.post("/api/agents/event", json={**base, "event": "UserPromptSubmit"})
    response = client.post("/api/agents/event", json={**base, "event": "SessionStart"}).json()["agent"]
    assert response["state"] == "working"
    running = client.post("/api/agents/event", json={**base, "event": "PreToolUse", "tool_use_id": "t"}).json()["agent"]
    assert running["activity_label"] == "Running command"
    client.post("/api/agents/event", json={**base, "event": "PostToolUse", "tool_use_id": "t"})
    response = client.post("/api/agents/event", json={**base, "event": "Stop", "background_tasks": ["job"]}).json()["agent"]
    assert response["state"] == "working" and response["activity_label"] == "Background work"
    response = client.post("/api/agents/event", json={**base, "event": "SessionEnd"}).json()["agent"]
    assert response["state"] == "working"
    response = client.post("/api/agents/event", json={**base, "event": "Stop", "background_tasks": []}).json()["agent"]
    assert response["state"] == "done" and response["background_count"] == 0
    assert client.delete("/api/agents/c").status_code == 200
    assert not client.app.state.agents.agents


def test_replay_deduplicates_and_cannot_resurrect_dismissed_results(
    tmp_path: Path,
    ) -> None:
    """
    Persist acknowledgment across restarts while accepting a genuinely newer task.

    Args:
        tmp_path (Path): Isolated local state directory.

    Returns:
        None: Replayed old events stay hidden and fresh activity creates a new tile.
    """
    async def exercise() -> None:
        """
        Exercise the registry through a persistent dismissal and a new registry instance.

        Returns:
            None: Replay and new-task assertions pass.
        """
        hub = Hub()
        path = tmp_path / "dismissals.json"
        registry = AgentRegistry(hub, {}, state_path=path)
        event = AgentEvent(session_id="s", event="Stop", event_id="one", activity_at=1)
        await registry.handle(event)
        await registry.handle(event)
        assert len(registry.seen) == 1
        await registry.dismiss("s")
        registry = AgentRegistry(Hub(), {}, state_path=path)
        await registry.handle(event)
        assert not registry.agents
        await registry.handle(AgentEvent(session_id="s", event="UserPromptSubmit"))
        assert registry.agents["s"]["state"] == "working"
    asyncio.run(exercise())


# --- Durable metadata and connection ownership ---


def test_measurements_do_not_reorder_lifecycle_and_restore_is_not_live() -> None:
    """
    Preserve hook ordering across background polling and avoid claiming restored work is live.

    Returns:
        None: A delayed Stop still completes and restored observations require fresh confirmation.
    """
    async def exercise() -> None:
        """
        Interleave a newer job measurement with a delayed lifecycle delivery.

        Returns:
            None: The registry preserves source ordering and truthful restoration behavior.
        """
        registry = AgentRegistry(Hub(), {})
        await registry.handle(AgentEvent(session_id="s", event="UserPromptSubmit", activity_at=1, replay=True))
        await registry.finish_restore()
        assert registry.agents["s"]["state"] == "unknown"
        await registry.handle(AgentEvent(session_id="s", event="PreToolUse", activity_at=2, tool_use_id="t"))
        assert registry.agents["s"]["state"] == "working"
        await registry.handle(AgentEvent(session_id="s", event="BackgroundUpdate", activity_at=4, background_tasks=[]))
        await registry.handle(AgentEvent(session_id="s", event="Stop", activity_at=3))
        assert registry.agents["s"]["state"] == "done"
        await registry.handle(AgentEvent(session_id="s", event="Interrupt", activity_at=5))
        await registry.handle(AgentEvent(session_id="s", event="BackgroundUpdate", background_tasks=[]))
        assert registry.agents["s"]["state"] == "error"
        await registry.handle(AgentEvent(session_id="closed", event="UserPromptSubmit"))
        await registry.handle(AgentEvent(session_id="closed", event="SessionEnd"))
        assert registry.agents["closed"]["state"] == "unknown"
        await registry.dismiss("closed")
        await registry.handle(AgentEvent(session_id="active", event="PreToolUse", tool_use_id="live"))
        await registry.dismiss("active")
        assert "active" not in registry.agents
        await registry.handle(AgentEvent(session_id="active", event="PostToolUse", tool_use_id="live",
                                         activity_at=registry.dismissed["active"] + 1))
        assert registry.agents["active"]["state"] == "working"
    asyncio.run(exercise())


def test_concurrent_hook_events_keep_identity_and_destination(
    tmp_path: Path,
    ) -> None:
    """
    Serialize concurrent hook writers and isolate deliberately remote display destinations.

    Args:
        tmp_path (Path): Isolated hook configuration directory.

    Returns:
        None: Every queued event is delivered once with its original identity and timestamp.
    """
    env = {"PIDECK_CONFIG": str(tmp_path / "hook.json")}
    def write(
        number: int,
        ) -> dict[str, Any]:
        """
        Append one sanitized lifecycle event from a concurrent hook process.

        Args:
            number (int): Unique test session number.

        Returns:
            dict[str, Any]: Stored event carrying its delivery identity.
        """
        return remember({"session_id": str(number), "event": "UserPromptSubmit"}, "http://localhost:8765", env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        written = list(pool.map(write, range(20)))
    queued = pending("http://127.0.0.1:8765/", 0, env)
    assert len(queued) == 20
    assert {event["event_id"] for event in written} == {event["event_id"] for _, event in queued}
    assert pending("https://example.com", 0, env) == []
    assert pending("http://localhost:8765", queued[-1][0], env) == []


def test_local_repair_updates_owned_hooks_and_preserves_remote_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Repair stale local credentials and helper paths without replacing an intentional remote setup.

    Args:
        tmp_path (Path): Isolated user home with existing provider settings.
        monkeypatch (pytest.MonkeyPatch): Temporary environment and home-directory overrides.

    Returns:
        None: Local connection is repaired and a later remote destination remains untouched.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PIDECK_HOOK_TOKEN", "fresh-hook-only")
    config = tmp_path / ".config/pideck/hook.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"url": "http://localhost:8765", "token": "old"}))
    provider = tmp_path / ".claude/settings.json"
    provider.parent.mkdir()
    provider.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "old pideck_hook.py"}]}]},
                                    "statusLine": {"command": "old pideck_hook.py --statusline", "refreshInterval": 60}}))
    assert synchronize_hooks("http://127.0.0.1:8765/")
    saved = json.loads(config.read_text())
    assert saved["token"] == "fresh-hook-only"
    assert "old " not in json.loads(provider.read_text())["statusLine"]["command"]
    assert (config.parent / "event_store.py").is_file()
    assert (tmp_path / ".claude/mods/pideck-usage/hooks/register.ts").is_file()
    saved["url"] = "https://my-pi.example.com"
    config.write_text(json.dumps(saved))
    before = config.read_bytes()
    assert not synchronize_hooks("http://127.0.0.1:8765/")
    assert config.read_bytes() == before


# --- Verified-schema local metadata ---


def test_codex_receipt_requires_matching_turn_and_known_schema(
    tmp_path: Path,
    ) -> None:
    """
    Refuse to dismiss a new completion using an older read receipt or an unfamiliar schema.

    Args:
        tmp_path (Path): Isolated Codex home.

    Returns:
        None: Only the exact published and acknowledged turn qualifies for dismissal.
    """
    path = tmp_path / ".codex/state_5.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE thread_read_receipts (thread_id TEXT, first_unread_turn TEXT, revision TEXT, last_published_turn TEXT)")
        connection.execute("INSERT INTO thread_read_receipts VALUES ('s', 'new', '1', 'new')")
    assert not codex_viewed("s", "new", tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE thread_read_receipts SET first_unread_turn = NULL")
    assert codex_viewed("s", "new", tmp_path)
    assert not codex_viewed("s", "old", tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE thread_read_receipts ADD COLUMN changed_semantics TEXT")
    assert not codex_viewed("s", "new", tmp_path)


def test_cursor_background_waits_for_structured_exit_footer(
    tmp_path: Path,
    ) -> None:
    """
    Ignore Cursor's transient aborted view status and follow the reported background terminal.

    Args:
        tmp_path (Path): Isolated Cursor home and read-only source database.

    Returns:
        None: Context remains available and only a complete terminal footer clears the job.
    """
    database = tmp_path / "state.vscdb"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                           ("composerData:s", json.dumps({"status": "aborted", "hasUnreadMessages": False, "contextUsagePercent": 42})))
        connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                           ("bubbleId:s:one", json.dumps({"toolFormerData": {"result": json.dumps({"backgroundShellId": "123"})}})))
    env = {"PIDECK_HOME": str(tmp_path), "CURSOR_STATE_DB": str(database)}
    assert cursor_metrics("s", env) == {"context_pct": 42, "background_tasks": ["123"]}
    output = tmp_path / ".cursor/projects/project/terminals/123.txt"
    output.parent.mkdir(parents=True)
    output.write_text("BACKGROUND_DONE\n")
    assert cursor_metrics("s", env)["background_tasks"] == ["123"]
    output.write_text("BACKGROUND_DONE\n\n---\nexit_code: 0\nelapsed_ms: 45000\nended_at: 2026-11-09T22:00:00Z\n---\n")
    other = tmp_path / ".cursor/projects/other/terminals/123.txt"
    other.parent.mkdir(parents=True)
    other.write_text("still running\n", encoding="utf-8")
    assert cursor_metrics("s", env)["background_tasks"] == ["123"]
    other.unlink()
    assert cursor_metrics("s", env) == {"context_pct": 42, "background_tasks": []}
