"""

Cursor lifecycle, context snapshots, privacy, and installation coverage.

"""

from __future__ import annotations

from typing import Any
from fastapi.testclient import TestClient

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import install
import pideck_hook as hook


# --- Cursor payloads and registry ---


@pytest.mark.parametrize("event, expected", [
    ("sessionStart", "idle"), ("beforeSubmitPrompt", "working"),
    ("postToolUse", "working"), ("stop", "done"),
])
def test_cursor_hooks_roundtrip(
    client: TestClient,
    event: Any,
    expected: Any,
    ) -> None:
    """
    Map documented Cursor hook names and stable conversation IDs to provider tiles.

    Args:
        client (TestClient): Authenticated test client for the isolated application.
        event (Any): Provider event name or event fields to handle.
        expected (Any): Expected normalized value or response fields for this test case.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    payload = {"hook_event_name": event, "conversation_id": "conversation", "generation_id": "turn",
               "workspace_roots": ["C:/work/project"], "status": "completed",
               "prompt": "private prompt", "user_email": "private@example.com", "tool_input": {"secret": "private"}}
    outgoing = hook.build_event(payload, {}, "cursor")
    assert "private" not in json.dumps(outgoing)
    response = client.post("/api/agents/event", json=outgoing)
    assert response.status_code == 200
    agent = response.json()["agent"]
    assert (agent["id"], agent["agent"], agent["name"], agent["state"]) == (
        "cursor:conversation", "cursor", "project", expected)


def test_cursor_compaction_snapshot_is_cleared_by_later_activity(
    client: TestClient,
    ) -> None:
    """
    Use Cursor's reported context snapshot, then clear it rather than implying live usage.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    base = {"conversation_id": "c", "workspace_roots": ["/work/project"]}
    start = hook.build_event({**base, "hook_event_name": "beforeSubmitPrompt"}, {}, "cursor")
    client.post("/api/agents/event", json=start)
    compact = hook.build_event({**base, "hook_event_name": "preCompact", "context_usage_percent": 85}, {}, "cursor")
    agent = client.post("/api/agents/event", json=compact).json()["agent"]
    assert agent["state"] == "working" and agent["context_pct"] == 85
    stop = hook.build_event({**base, "hook_event_name": "stop", "status": "completed"}, {}, "cursor")
    agent = client.post("/api/agents/event", json=stop).json()["agent"]
    assert agent["context_pct"] is None and agent["state"] == "done"
    end = hook.build_event({"session_id": "c", "hook_event_name": "sessionEnd"}, {}, "cursor")
    assert client.post("/api/agents/event", json=end).json()["agent"] is None
    assert client.app.state.hub.state["agents"] == []


@pytest.mark.parametrize("payload, expected", [
    ({"hook_event_name": "stop", "status": "error"}, "error"),
    ({"hook_event_name": "stop", "status": "aborted"}, "idle"),
    ({"hook_event_name": "postToolUseFailure", "failure_type": "permission_denied"}, "waiting"),
])
def test_cursor_failures_and_cancellation(
    client: TestClient,
    payload: dict[str, Any],
    expected: Any,
    ) -> None:
    """
    Represent errors, cancellation, and denied tools without inventing successful completion.

    Args:
        client (TestClient): Authenticated test client for the isolated application.
        payload (dict[str, Any]): Decoded provider payload to inspect or translate.
        expected (Any): Expected normalized value or response fields for this test case.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    event = hook.build_event({**payload, "conversation_id": "c"}, {}, "cursor")
    assert client.post("/api/agents/event", json=event).json()["agent"]["state"] == expected


def test_cursor_detail_names_the_model_and_keeps_prompts_off_the_tile(
    client: TestClient,
    ) -> None:
    """
    Two chats in one project stay distinct via a public model label, not the prompt.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c",
               "workspace_roots": ["/work/project"], "model": "composer-2.5", "composer_mode": "ask",
               "prompt": "private prompt", "user_email": "private@example.com"}
    event = hook.build_event(payload, {}, "cursor")
    assert event["detail"] == "ask · composer-2.5"
    assert "private" not in json.dumps(event)
    agent = client.post("/api/agents/event", json=event).json()["agent"]
    assert agent["detail"] == "ask · composer-2.5" and agent["name"] == "project"
    later = hook.build_event({"hook_event_name": "stop", "conversation_id": "c", "status": "completed"}, {}, "cursor")
    kept = client.post("/api/agents/event", json=later).json()["agent"]
    assert kept["detail"] == "ask · composer-2.5" and "private" not in json.dumps(kept)


def test_cursor_subagent_is_tagged_to_its_parent_without_the_task() -> None:
    """
    A child agent is its own tile, linked to the parent, and its task stays local.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    payload = {"hook_event_name": "subagentStart", "subagent_id": "child",
               "parent_conversation_id": "parent", "subagent_type": "generalPurpose",
               "subagent_model": "composer-2.5", "task": "private task text",
               "workspace_roots": ["C:/work/project"]}
    event = hook.build_event(payload, {}, "cursor")
    assert event["session_id"] == "cursor:sub:child"
    assert event["parent_id"] == "cursor:parent"
    assert (event["name"], event["detail"], event["event"]) == ("generalPurpose", "composer-2.5", "UserPromptSubmit")
    assert "private" not in json.dumps(event)
    assert hook.cursor_hook_response("subagentStart") == {"permission": "allow"}
    stop = hook.build_event({"hook_event_name": "subagentStop", "subagent_id": "child",
                             "parent_conversation_id": "parent", "subagent_type": "generalPurpose",
                             "status": "completed", "summary": "private summary"}, {}, "cursor")
    assert stop["session_id"] == "cursor:sub:child" and stop["event"] == "Stop"
    assert "private" not in json.dumps(stop)


def test_claude_subagent_keeps_the_parent_session_id() -> None:
    """
    Claude reports the parent session_id; the child tile must not replace that chat.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    event = hook.build_event({
        "hook_event_name": "SubagentStart", "session_id": "parent", "agent_id": "child",
        "agent_type": "Explore", "cwd": "/work/project", "last_assistant_message": "private reply",
    }, {}, "claude")
    assert event["session_id"] == "parent:child" and event["parent_id"] == "parent"
    assert event["name"] == "Explore" and event["event"] == "UserPromptSubmit"
    assert "private" not in json.dumps(event)


def test_cursor_ignores_unrelated_hooks_and_invalid_context() -> None:
    """
    Missing conversation identity and non-agent hooks cannot create anonymous tiles.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert hook.build_event({"hook_event_name": "workspaceOpen"}, {}, "cursor") is None
    assert hook.build_event({"hook_event_name": "stop"}, {}, "cursor") is None
    event = hook.build_event({"hook_event_name": "preCompact", "conversation_id": "c",
                              "context_usage_percent": 200}, {}, "cursor")
    assert event["context_pct"] is None and "usage" not in event


@pytest.mark.parametrize("event, expected", [
    ("beforeSubmitPrompt", {"continue": True}), ("postToolUse", {}),
])
def test_cursor_returns_valid_hook_json_when_display_is_offline(
    event: Any,
    expected: Any,
    ) -> None:
    """
    An unavailable piDeck must not turn the prompt hook into a blocking invalid response.

    Args:
        event (Any): Provider event name or event fields to handle.
        expected (Any): Expected normalized value or response fields for this test case.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    script = Path(__file__).parent.parent / "hooks" / "pideck_hook.py"
    result = subprocess.run([sys.executable, str(script), "--source", "cursor"],
                            input=json.dumps({"hook_event_name": event, "conversation_id": "c"}),
                            text=True, capture_output=True, timeout=5,
                            env={**os.environ, "PIDECK_URL": "http://127.0.0.1:1"})
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == expected


# --- Cursor installation ---


def test_cursor_install_is_idempotent_and_preserves_user_hooks(
    tmp_path: Path,
    ) -> None:
    """
    Install flat Cursor hooks, retain custom entries, and remove only piDeck on uninstall.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    path = tmp_path / ".cursor" / "hooks.json"
    path.parent.mkdir()
    original = {"version": 1, "custom": True, "hooks": {
        "stop": [{"command": "echo original"}], "afterFileEdit": [{"command": "echo format"}]}}
    path.write_text(json.dumps(original))
    args = ["--url", "http://localhost:8765", "--no-check", "--cursor"]
    assert install.main(args, home=tmp_path) == 0
    first = path.read_text()
    settings = json.loads(first)
    assert settings["hooks"]["stop"][0] == original["hooks"]["stop"][0]
    assert settings["hooks"]["stop"][1]["command"].endswith("--source cursor")
    assert "hooks" not in settings["hooks"]["stop"][1]
    assert install.main(args, home=tmp_path) == 0
    assert path.read_text() == first
    assert install.main(["--uninstall"], home=tmp_path) == 0
    assert json.loads(path.read_text()) == original


def test_cursor_autodetection_and_explicit_provider_selection(
    tmp_path: Path,
    ) -> None:
    """
    Auto-detect Cursor while explicit flags restrict installation to selected providers.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    for folder in (".cursor", ".claude", ".codex"):
        (tmp_path / folder).mkdir()
    base = ["--url", "http://localhost:8765", "--no-check"]
    assert install.main([*base, "--cursor"], home=tmp_path) == 0
    assert (tmp_path / ".cursor/hooks.json").exists()
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert install.main(base, home=tmp_path) == 0
    assert (tmp_path / ".claude/settings.json").exists()
    assert (tmp_path / ".codex/hooks.json").exists()


def test_cursor_dry_run_and_unsupported_config_are_unchanged(
    tmp_path: Path,
    ) -> None:
    """
    Dry runs and unsupported Cursor schemas must leave the user's file untouched.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    path = tmp_path / ".cursor/hooks.json"
    path.parent.mkdir()
    original = '{"version": 1, "hooks": {}}'
    path.write_text(original)
    args = ["--url", "http://localhost:8765", "--no-check", "--cursor", "--dry-run"]
    assert install.main(args, home=tmp_path) == 0
    assert path.read_text() == original
    assert not (tmp_path / ".config").exists()
    path.write_text('{"version": 9, "hooks": {}}')
    with pytest.raises(install.InstallError):
        install.update_cursor_hooks(path, tmp_path / "pideck_hook.py", False)
    assert json.loads(path.read_text())["version"] == 9
