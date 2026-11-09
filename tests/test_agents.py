"""

Agent lifecycle, session registry, and websocket alert integration tests.

"""

from __future__ import annotations

from typing import Any
import httpx

import pytest
from fastapi.testclient import TestClient

from pideck.main import create_app


# --- Tests ---

def post(
    client: TestClient,
    **kw: Any,
    ) -> httpx.Response:
    """
    Post a lifecycle event to the isolated agent API.

    Args:
        client (TestClient): Authenticated test client for the isolated application.
        **kw (Any): Additional arguments accepted by this callback or forwarded to the wrapped
            operation.

    Returns:
        httpx.Response: HTTP response from the agent lifecycle endpoint.
    """
    return client.post("/api/agents/event", json=kw)


def test_lifecycle_and_alerts(
    client: TestClient,
    ) -> None:
    """
    Publish lifecycle states and alerts that identify their owning session.

    Args:
        client (TestClient): Authenticated test client connected to an isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with client.websocket_connect("wss://testserver/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"

        post(client, session_id="s1", event="SessionStart", cwd="/home/me/api-gateway")
        assert ws.receive_json()["value"][0]["state"] == "idle"

        post(client, session_id="s1", event="UserPromptSubmit")
        a = ws.receive_json()["value"][0]
        assert (a["state"], a["name"]) == ("working", "api-gateway")

        post(client, session_id="s1", event="Notification", message="needs permission")
        assert ws.receive_json()["value"][0]["state"] == "waiting"
        alert = ws.receive_json()
        assert alert["type"] == "alert" and alert["alert"]["title"] == "api-gateway needs you"

        post(client, session_id="s1", event="PostToolUse")  # approved, back to work
        assert ws.receive_json()["value"][0]["state"] == "working"
        post(client, session_id="s1", event="Stop", context_pct=42)
        a = ws.receive_json()["value"][0]
        assert (a["state"], a["context_pct"]) == ("done", 42)
        alert = ws.receive_json()["alert"]
        assert alert["title"] == "api-gateway finished"
        assert alert["session_id"] == "s1"

        post(client, session_id="s1", event="SessionEnd")
        assert ws.receive_json()["value"] == []


def test_fresh_session_settling_is_not_an_alert(
    client: TestClient,
    ) -> None:
    """
    Verify fresh session settling is not an alert.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with client.websocket_connect("wss://testserver/ws") as ws:
        ws.receive_json()
        post(client, session_id="x", event="Stop")  # e.g. a session we never saw start
        assert ws.receive_json()["type"] == "state"
        # no alert follows: next message must be our own next event
        post(client, session_id="x", event="UserPromptSubmit")
        assert ws.receive_json()["value"][0]["state"] == "working"


def test_validation(
    client: TestClient,
    ) -> None:
    """
    Verify validation.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert post(client, session_id="s").status_code == 422                      # no event/state
    assert post(client, session_id="s", state="bogus").status_code == 422
    assert post(client, session_id="s", event="SomethingNew").status_code == 200  # ignored quietly
    assert client.get("/api/health").json()["ok"] is True


def test_context_pct_sticks_between_events(
    client: TestClient,
    ) -> None:
    """
    Verify context pct sticks between events.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    post(client, session_id="s", event="UserPromptSubmit", context_pct=30)
    r = post(client, session_id="s", event="PostToolUse")  # hook couldn't compute it this time
    assert r.json()["agent"]["context_pct"] == 30


def test_new_client_gets_snapshot(
    client: TestClient,
    ) -> None:
    """
    Verify new client gets snapshot.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    post(client, session_id="s", event="UserPromptSubmit", cwd="/a/b")
    with client.websocket_connect("wss://testserver/ws") as ws:
        snap = ws.receive_json()
        assert snap["state"]["agents"][0]["name"] == "b"


def test_codex_kind_sticks_and_alerts_carry_it(
    client: TestClient,
    ) -> None:
    """
    Verify codex kind sticks and alerts carry it.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with client.websocket_connect("wss://testserver/ws") as ws:
        ws.receive_json()
        post(client, session_id="c1", event="UserPromptSubmit", agent="codex", cwd="/w/site")
        a = ws.receive_json()["value"][0]
        assert (a["agent"], a["state"], a["context_pct"]) == ("codex", "working", None)
        post(client, session_id="c1", event="PostToolUse")          # later events needn't repeat it
        assert ws.receive_json()["value"][0]["agent"] == "codex"
        post(client, session_id="c1", event="Stop", message="all done")
        assert ws.receive_json()["value"][0]["state"] == "done"
        alert = ws.receive_json()["alert"]
        assert (alert["agent"], alert["title"], alert["body"]) == ("codex", "site finished", "all done")


def test_unknown_agent_kind_falls_back_to_claude(
    client: TestClient,
    ) -> None:
    """
    Verify unknown agent kind falls back to claude.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    r = post(client, session_id="u", event="UserPromptSubmit", agent="gpt-99")
    assert r.json()["agent"]["agent"] == "claude"


def test_permission_request_means_waiting(
    client: TestClient,
    ) -> None:
    """
    Verify permission request means waiting.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    post(client, session_id="p", event="UserPromptSubmit")
    r = post(client, session_id="p", event="PermissionRequest", message="Run npm test?")
    assert r.json()["agent"]["state"] == "waiting"


def test_config_exposes_rotate(
    client: TestClient,
    ) -> None:
    """
    Verify config exposes rotate.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert client.get("/api/config").json()["rotate"] == 0


def test_noop_prime_does_not_refresh_prune_clock(
    client: TestClient,
    ) -> None:
    """
    Rediscovering an unchanged idle session must keep activity_at and updated_at stable.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    first = post(
        client, session_id="idle-1", state="idle", name="demo", agent="claude", activity_at=1_000.0,
    ).json()["agent"]
    assert first["activity_at"] == 1_000.0
    again = post(
        client, session_id="idle-1", state="idle", name="demo", agent="claude", activity_at=1_000.0,
    ).json()["agent"]
    assert again["updated_at"] == first["updated_at"]
    assert again["activity_at"] == 1_000.0
    live = post(client, session_id="idle-1", event="UserPromptSubmit", name="demo", agent="claude")
    agent = live.json()["agent"]
    assert agent["state"] == "working"
    assert agent["activity_at"] >= again["activity_at"]
    assert agent["updated_at"] >= again["updated_at"]
    # Further live hooks while already working still advance activity (not a prime no-op).
    busy = post(client, session_id="idle-1", event="PostToolUse", name="demo", agent="claude").json()["agent"]
    assert busy["state"] == "working"
    assert busy["activity_at"] >= agent["activity_at"]
    assert busy["updated_at"] >= agent["updated_at"]


def test_usage_note_is_published_without_waking_tiles(
    client: TestClient,
    ) -> None:
    """
    A failed provider fetch can publish a short note without recreating sessions.

    Args:
        client (TestClient): Authenticated test client for the isolated application.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    post(client, session_id="s", state="waiting", agent="claude")
    response = post(
        client, session_id="usage:claude", event="UsageUpdate", agent="claude", note="http_503",
    )
    assert response.status_code == 200
    assert client.app.state.hub.state["agent_usage"]["claude"]["note"] == "http_503"
    assert client.app.state.agents.agents["s"]["state"] == "waiting"
