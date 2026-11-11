"""

Regress real lifecycle boundaries, offline replay, authentication repair, and manual dismissal.

"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from hooks.event_store import pending, remember
from hooks.runtime import cursor_metrics
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
    running = client.post("/api/agents/event", json={**base, "event": "PreToolUse", "tool_use_id": "t", "tool_name": "Bash"}).json()["agent"]
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
        now = time.time()
        await registry.handle(AgentEvent(session_id="s", event="UserPromptSubmit", activity_at=now, replay=True))
        await registry.finish_restore()
        assert registry.agents["s"]["state"] == "unknown"
        await registry.handle(AgentEvent(session_id="s", event="PreToolUse", activity_at=now + 2, tool_use_id="t"))
        assert registry.agents["s"]["state"] == "working"
        await registry.handle(AgentEvent(session_id="s", event="BackgroundUpdate", activity_at=now + 4, background_tasks=[]))
        await registry.handle(AgentEvent(session_id="s", event="Stop", activity_at=now + 3))
        assert registry.agents["s"]["state"] == "done"
        await registry.handle(AgentEvent(session_id="s", event="Interrupt", activity_at=now + 5))
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


@pytest.mark.parametrize("provider", ["claude", "codex", "cursor"])
def test_children_keep_tool_identity_separate_from_parent(
    client: TestClient,
    provider: str,
    ) -> None:
    """
    Route two same-type workers and their concurrent tools without changing the parent.

    Args:
        client (TestClient): Isolated authenticated application client.
        provider (str): Provider whose documented identity fields are exercised.

    Returns:
        None: Child tools, failures, and completion remain isolated by stable identity.
    """
    from hooks.pideck_hook import build_event

    parent = "cursor:parent" if provider == "cursor" else "parent"
    client.post("/api/agents/event", json={"session_id": parent, "event": "UserPromptSubmit", "agent": provider})
    for child in ("one", "two"):
        base = ({"conversation_id": "parent", "parent_conversation_id": "parent", "subagent_id": child}
                if provider == "cursor" else {"session_id": "parent", "agent_id": child})
        start = "subagentStart" if provider == "cursor" else "SubagentStart"
        payload = {**base, "hook_event_name": start, "agent_type": "worker", "subagent_type": "worker"}
        event = build_event(payload, {}, provider)
        client.post("/api/agents/event", json=event)
        tool = build_event({**base, "hook_event_name": "preToolUse" if provider == "cursor" else "PreToolUse",
                            "tool_name": "Shell" if provider == "cursor" else "Bash", "tool_use_id": child,
                            "tool_input": {"command": "private command"}}, {}, provider)
        running = client.post("/api/agents/event", json=tool).json()["agent"]
        assert running["parent_id"] == parent and running["running_tool_count"] == 1
        assert running["activity_label"] == "Running command"
        assert "private" not in json.dumps(running)
    registry = client.app.state.agents
    assert registry.agents[parent]["running_tool_count"] == 0
    assert len(registry.agents) == 3
    one = "cursor:sub:one" if provider == "cursor" else "parent:one"
    two = "cursor:sub:two" if provider == "cursor" else "parent:two"
    client.post("/api/agents/event", json={"session_id": one, "event": "PostToolUseFailure", "tool_use_id": "one"})
    assert registry.agents[one]["running_tool_count"] == 0
    assert registry.agents[two]["running_tool_count"] == 1
    client.post("/api/agents/event", json={"session_id": one, "event": "Stop"})
    assert registry.agents[parent]["state"] == "working"
    assert registry.agents[two]["state"] == "working"


def test_tools_are_not_all_shell_commands(
    client: TestClient,
    ) -> None:
    """
    Distinguish generic tools from command execution and retain concurrent tool names.

    Args:
        client (TestClient): Isolated authenticated application client.

    Returns:
        None: Finishing one command preserves other work and clears its command label.
    """
    base = {"session_id": "tools", "event": "PreToolUse"}
    read = client.post("/api/agents/event", json={**base, "tool_use_id": "read", "tool_name": "Read"}).json()["agent"]
    assert read["activity_label"] == "Using tool" and read["active_tools"] == ["Read"]
    shell = client.post("/api/agents/event", json={**base, "tool_use_id": "shell", "tool_name": "Bash"}).json()["agent"]
    assert shell["activity_label"] == "Running command" and shell["running_tool_count"] == 2
    after = client.post("/api/agents/event", json={"session_id": "tools", "event": "PostToolUse", "tool_use_id": "shell"}).json()["agent"]
    assert after["activity_label"] == "Using tool" and after["active_tools"] == ["Read"]


def test_cursor_missing_child_identity_never_finishes_parent() -> None:
    """
    Reject ambiguous child events instead of guessing identity from a shared type.

    Returns:
        None: Neither starts nor stops without stable child IDs become parent activity.
    """
    from hooks.pideck_hook import build_event

    for event in ("subagentStart", "subagentStop"):
        assert build_event({"hook_event_name": event, "conversation_id": "parent",
                            "subagent_type": "worker"}, {}, "cursor") is None


def test_cursor_provider_metadata_links_real_child_conversations(
    tmp_path: Path,
    client: TestClient,
    ) -> None:
    """
    Match Cursor's live subagentInfo schema to lifecycle IDs without reading task content.

    Args:
        tmp_path (Path): Isolated provider database directory.
        client (TestClient): Isolated authenticated application client.

    Returns:
        None: Two workers with long shared ID prefixes retain independent tools and completion.
    """
    from hooks.pideck_hook import build_event
    from hooks.runtime import cursor_child_identity

    db = tmp_path / "state.vscdb"
    env = {"CURSOR_STATE_DB": str(db)}
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        for child in ("one", "two"):
            tool = "x" * 150 + child
            connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:" + child,
                json.dumps({"subagentInfo": {"parentComposerId": "parent", "toolCallId": tool},
                            "private": "must not forward"})))
    identities = []
    for child in ("one", "two"):
        start = build_event({"hook_event_name": "subagentStart", "conversation_id": "parent",
                             "parent_conversation_id": "parent", "tool_call_id": "x" * 150 + child}, env, "cursor")
        client.post("/api/agents/event", json=start)
        tool = build_event({"hook_event_name": "preToolUse", "conversation_id": child,
                            "tool_name": "Shell", "tool_use_id": child}, env, "cursor")
        assert tool["session_id"] == start["session_id"]
        assert tool["parent_id"] == "cursor:parent" and tool["provider_session_id"] == child
        identities.append(tool["session_id"])
        client.post("/api/agents/event", json=tool)
        stop = build_event({"hook_event_name": "stop", "conversation_id": child,
                            "status": "completed", "background_tasks": []}, env, "cursor")
        done = client.post("/api/agents/event", json=stop).json()["agent"]
        assert done["state"] == "done" and done["running_tool_count"] == 0
        assert "private" not in json.dumps(done)
    assert len(set(identities)) == 2 and all(len(value) <= 128 for value in identities)
    assert cursor_child_identity("missing", env) is None
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", ("composerData:bad", "[]"))
    assert cursor_child_identity("bad", env) is None


def test_parent_completion_does_not_invent_child_completion(
    client: TestClient,
    ) -> None:
    """
    Keep uncertain children dismissible and hide a family's cards together when acknowledged.

    Args:
        client (TestClient): Isolated authenticated application client.

    Returns:
        None: Parent completion never fabricates a child's result or leaves dismissed orphans.
    """
    for sid, parent in (("p", None), ("c", "p"), ("g", "c")):
        client.post("/api/agents/event", json={"session_id": sid, "parent_id": parent, "event": "UserPromptSubmit"})
    client.post("/api/agents/event", json={"session_id": "p", "event": "Stop"})
    registry = client.app.state.agents
    assert registry.agents["c"]["state"] == "unknown"
    assert registry.agents["c"]["activity_label"] == "Status unknown"
    client.delete("/api/agents/p")
    assert not registry.agents
    assert {"p", "c", "g"} <= registry.dismissed.keys()


def test_claude_child_completion_ignores_session_background_list() -> None:
    """
    Finish a child even when Claude reports that child and siblings in the session-wide job list.

    Returns:
        None: Session jobs do not leak into the child's command or completion state.
    """
    from hooks.pideck_hook import build_event

    base = {"session_id": "parent", "agent_id": "child", "background_tasks": [{"id": "child"}, {"id": "sibling"}]}
    pre = build_event({**base, "hook_event_name": "PreToolUse", "tool_name": "PowerShell", "tool_use_id": "t"}, {}, "claude")
    assert "background_tasks" not in pre
    stop = build_event({**base, "hook_event_name": "SubagentStop"}, {}, "claude")
    assert stop["session_id"] == "parent:child" and stop["background_tasks"] == []


def test_child_attention_alerts_but_completion_stays_in_drawer(
    client: TestClient,
    ) -> None:
    """
    Retain child approval alerts without giving every child a full-screen completion takeover.

    Args:
        client (TestClient): Isolated authenticated application client.

    Returns:
        None: Child attention identifies the worker and child completion emits only state.
    """
    with client.websocket_connect("wss://testserver/ws") as ws:
        ws.receive_json()
        client.post("/api/agents/event", json={"session_id": "parent", "event": "UserPromptSubmit"})
        ws.receive_json()
        base = {"session_id": "child", "parent_id": "parent"}
        client.post("/api/agents/event", json={**base, "event": "UserPromptSubmit"})
        ws.receive_json()
        client.post("/api/agents/event", json={**base, "event": "PermissionRequest"})
        assert ws.receive_json()["type"] == "state"
        assert ws.receive_json()["alert"]["session_id"] == "child"
        client.post("/api/agents/event", json={**base, "event": "Stop"})
        assert ws.receive_json()["type"] == "state"
        client.post("/api/agents/event", json={"session_id": "parent", "event": "PostToolUse"})
        assert ws.receive_json()["type"] == "state"


# --- Startup snapshots and retained completions ---


def test_startup_restore_filters_expired_history_before_publishing(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Publish only the final recent snapshot while preserving work confirmed during restoration.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper capturing agent publications without a display.

    Returns:
        None: Expired cards never appear, recent completions remain, and fresh work stays working.
    """
    async def exercise() -> None:
        """
        Interleave expired history, recent results, and a live update during initial replay.

        Returns:
            None: One final, filtered agent snapshot is published.
        """
        hub = Hub()
        published = []
        async def capture(
            message: dict,
            ) -> None:
            """
            Record deep copies of agent snapshots rather than references to mutable session state.

            Args:
                message (dict): Hub message generated by the registry.

            Returns:
                None: Agent snapshots are retained for assertions.
            """
            if message.get("key") == "agents":
                published.append(json.loads(json.dumps(message["value"])))
        monkeypatch.setattr(hub, "_broadcast", capture)
        registry = AgentRegistry(hub, {"stale_hours": 1})
        registry.restoring = True
        now = time.time()
        for event in (
            AgentEvent(session_id="expired-result", event="Stop", activity_at=now - 7200, replay=True),
            AgentEvent(session_id="expired-work", event="PreToolUse", tool_use_id="old", background_tasks=["old-job"], activity_at=now - 7200, replay=True),
            AgentEvent(session_id="recent-result", event="Stop", agent="codex", activity_at=now - 60, replay=True),
            AgentEvent(session_id="unconfirmed-work", event="UserPromptSubmit", activity_at=now - 30, replay=True),
            AgentEvent(session_id="fresh-work", event="UserPromptSubmit", activity_at=now),
        ):
            await registry.handle(event)
        assert not published
        await registry.finish_restore()
        assert len(published) == 1
        assert {agent["id"] for agent in published[0]} == {"recent-result", "unconfirmed-work", "fresh-work"}
        assert registry.agents["recent-result"]["state"] == "done"
        assert registry.agents["unconfirmed-work"]["state"] == "unknown"
        assert registry.agents["fresh-work"]["state"] == "working"
        assert "expired-work" not in registry.tools and "expired-work" not in registry.jobs
        await registry.handle(AgentEvent(session_id="fresh-work", event="Stop"))
        assert len(published) == 2
    asyncio.run(exercise())


def test_monitor_replays_all_batches_and_keeps_viewed_completions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Finish startup replay before publishing and keep completed Codex cards after harness viewing.

    Args:
        tmp_path (Path): Isolated provider home and receipt database.
        monkeypatch (pytest.MonkeyPatch): Helper isolating hooks, queue reads, and background polling.

    Returns:
        None: Multiple replay batches yield one recent completed card despite a matching read receipt.
    """
    from pideck import local_prime

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    with sqlite3.connect(home / ".codex/state_5.sqlite") as connection:
        connection.execute("CREATE TABLE thread_read_receipts (thread_id TEXT, first_unread_turn TEXT, revision TEXT, last_published_turn TEXT)")
        connection.execute("INSERT INTO thread_read_receipts VALUES ('recent', NULL, '1', 'turn')")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(local_prime, "synchronize_hooks", lambda address: False)
    hub = Hub()
    registry = AgentRegistry(hub, {})
    reads = []
    now = time.time()
    def queued(
        address: str,
        after: int,
        env: dict,
        ) -> list[tuple[int, dict]]:
        """
        Return an old full page followed by one recent completion.

        Args:
            address (str): Display destination requested by the monitor.
            after (int): Previously consumed journal sequence number.
            env (dict): Runtime environment unused by this isolated queue.

        Returns:
            list[tuple[int, dict]]: Sequenced events simulating a multi-page startup journal.
        """
        reads.append(after)
        assert hub.state.get("agents", []) == []
        if after == 0:
            return [(i, {"session_id": "expired", "event": "Stop", "agent": "codex", "activity_at": now - 172800}) for i in range(1, 2001)]
        return [(2001, {"session_id": "recent", "event": "Stop", "agent": "codex", "turn_id": "turn", "activity_at": now - 10})]
    async def allowances(
        agents: AgentRegistry,
        ) -> None:
        """
        Wait without opening provider connections while the monitor is exercised.

        Args:
            agents (AgentRegistry): Isolated registry whose account polling is disabled.

        Returns:
            None: Runs until cancelled by monitor cleanup.
        """
        await asyncio.Event().wait()
    async def stop(
        seconds: float,
        ) -> None:
        """
        End the test after the monitor completes its first live polling iteration.

        Args:
            seconds (float): Requested polling delay, unused by the test.

        Returns:
            None: Never returns normally; cancellation exercises monitor cleanup.
        """
        raise asyncio.CancelledError
    monkeypatch.setattr(local_prime, "pending", queued)
    monkeypatch.setattr(local_prime, "refresh_allowances", allowances)
    monkeypatch.setattr(local_prime.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(local_prime.monitor(registry, lambda: "http://localhost:8765"))
    assert reads == [0, 2000]
    assert [agent["id"] for agent in hub.state["agents"]] == ["recent"]
    assert registry.agents["recent"]["state"] == "done"
