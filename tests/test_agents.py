import pytest
from fastapi.testclient import TestClient

from pideck.main import create_app


def post(client, **kw):
    return client.post("/api/agents/event", json=kw)


def test_lifecycle_and_alerts(client):
    with client.websocket_connect("/ws") as ws:
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
        assert ws.receive_json()["alert"]["title"] == "api-gateway finished"

        post(client, session_id="s1", event="SessionEnd")
        assert ws.receive_json()["value"] == []


def test_fresh_session_settling_is_not_an_alert(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        post(client, session_id="x", event="Stop")  # e.g. a session we never saw start
        assert ws.receive_json()["type"] == "state"
        # no alert follows: next message must be our own next event
        post(client, session_id="x", event="UserPromptSubmit")
        assert ws.receive_json()["value"][0]["state"] == "working"


def test_validation(client):
    assert post(client, session_id="s").status_code == 422                      # no event/state
    assert post(client, session_id="s", state="bogus").status_code == 422
    assert post(client, session_id="s", event="SomethingNew").status_code == 200  # ignored quietly
    assert client.get("/api/health").json()["ok"] is True


def test_context_pct_sticks_between_events(client):
    post(client, session_id="s", event="UserPromptSubmit", context_pct=30)
    r = post(client, session_id="s", event="PostToolUse")  # hook couldn't compute it this time
    assert r.json()["agent"]["context_pct"] == 30


def test_token_required_when_set(monkeypatch):
    monkeypatch.setenv("PIDECK_TOKEN", "sekret")
    monkeypatch.chdir("/tmp")
    with TestClient(create_app("/nonexistent/config.yaml")) as c:
        body = {"session_id": "s", "event": "SessionStart"}
        assert c.post("/api/agents/event", json=body).status_code == 401
        assert c.post("/api/agents/event", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.post("/api/agents/event", json=body, headers={"Authorization": "Bearer sekret"}).status_code == 200


def test_new_client_gets_snapshot(client):
    post(client, session_id="s", event="UserPromptSubmit", cwd="/a/b")
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["state"]["agents"][0]["name"] == "b"
