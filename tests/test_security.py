"""Who is allowed to read from or control the display."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pideck.main import create_app
from pideck.sources.agents import MAX_SESSIONS

BODY = {"session_id": "s", "event": "SessionStart"}


def make(monkeypatch, token="sekret", **kw):
    if token:
        monkeypatch.setenv("PIDECK_TOKEN", token)
    else:
        monkeypatch.delenv("PIDECK_TOKEN", raising=False)
    monkeypatch.chdir("/tmp")
    return TestClient(create_app("/nonexistent/config.yaml"), **kw)


def kiosk(monkeypatch, token="sekret"):
    """A request from the Pi's own browser: loopback peer, Host: localhost, no token."""
    return make(monkeypatch, token, base_url="http://localhost:8765", client=("127.0.0.1", 50000))


# TestClient gives websockets "Host: testserver", so say what a real browser would send.
KIOSK_WS = {"Host": "localhost:8765"}

ENDPOINTS = [("post", "/api/agents/event", BODY), ("delete", "/api/agents/s", None),
             ("post", "/api/spotify/pause", None), ("post", "/api/slack/clear", None)]


def call(c, method, path, body, **kw):
    return getattr(c, method)(path, **({"json": body} if body else {}), **kw)


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_remote_needs_the_token(monkeypatch, method, path, body):
    with make(monkeypatch) as c:
        assert call(c, method, path, body).status_code == 401
        assert call(c, method, path, body, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert call(c, method, path, body, headers={"Authorization": "Bearer sekret"}).status_code in (200, 409)


def test_websocket_needs_the_token_from_other_machines(monkeypatch):
    with make(monkeypatch) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws?token=nope"):
                pass
        with c.websocket_connect("/ws?token=sekret") as ws:      # how a phone or laptop browser connects
            assert ws.receive_json()["type"] == "snapshot"


def test_kiosk_on_this_machine_needs_no_token(monkeypatch):
    with kiosk(monkeypatch) as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 200
        with c.websocket_connect("/ws", headers=KIOSK_WS) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        assert c.post("/api/slack/clear").status_code == 200


def test_wrong_token_is_refused_even_from_this_machine(monkeypatch):
    with kiosk(monkeypatch) as c:
        assert c.post("/api/agents/event", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_dns_rebinding_host_is_not_treated_as_this_machine(monkeypatch):
    with make(monkeypatch, base_url="http://evil.example:8765", client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/slack/clear").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"Host": "evil.example:8765"}):
                pass


def test_cross_site_requests_are_refused_even_from_this_machine(monkeypatch):
    with kiosk(monkeypatch) as c:
        evil = {"Origin": "http://evil.example"}
        assert c.post("/api/slack/clear", headers=evil).status_code == 403
        assert c.post("/api/spotify/pause", headers=evil).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={**evil, **KIOSK_WS}):
                pass
        ok = {"Origin": "http://localhost:8765"}
        assert c.post("/api/slack/clear", headers=ok).status_code == 200
        with c.websocket_connect("/ws", headers={**ok, **KIOSK_WS}) as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_no_token_configured_means_only_this_machine(monkeypatch):
    with make(monkeypatch, token="") as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 401
        assert c.post("/api/agents/event", json=BODY, headers={"Authorization": "Bearer "}).status_code == 401
    with kiosk(monkeypatch, token="") as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 200


def test_security_headers_on_every_response(monkeypatch):
    with make(monkeypatch) as c:
        for path in ("/", "/api/health", "/app.js"):
            h = c.get(path).headers
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_oversized_and_out_of_range_fields_are_rejected(monkeypatch):
    with make(monkeypatch) as c:
        auth = {"Authorization": "Bearer sekret"}
        for bad in ({"name": "x" * 65}, {"message": "x" * 501}, {"cwd": "x" * 513},
                    {"context_pct": 150}, {"context_pct": -1}, {"session_id": "x" * 129}):
            body = {"session_id": "s", "event": "Stop", **bad}
            assert c.post("/api/agents/event", json=body, headers=auth).status_code == 422, bad


def test_session_count_is_capped(monkeypatch):
    with make(monkeypatch) as c:
        auth = {"Authorization": "Bearer sekret"}
        for i in range(MAX_SESSIONS + 5):
            c.post("/api/agents/event", json={"session_id": f"s{i}", "event": "UserPromptSubmit"}, headers=auth)
        assert len(c.app.state.agents.agents) == MAX_SESSIONS


def test_first_run_creates_and_saves_a_token(tmp_path, monkeypatch):
    import os
    from pideck.__main__ import ensure_token
    monkeypatch.delenv("PIDECK_TOKEN", raising=False)
    (tmp_path / ".env").write_text("SPOTIFY_CLIENT_ID=abc\nPIDECK_TOKEN=\n")      # as copied from .env.example
    tok, generated = ensure_token(str(tmp_path / "config.yaml"))
    assert generated and len(tok) >= 24 and os.environ["PIDECK_TOKEN"] == tok
    text = (tmp_path / ".env").read_text()
    assert "SPOTIFY_CLIENT_ID=abc" in text and text.count("PIDECK_TOKEN=") == 1 and f"PIDECK_TOKEN={tok}" in text
    if os.name == "posix":
        assert (tmp_path / ".env").stat().st_mode & 0o077 == 0                    # Unix permission bits
    assert ensure_token(str(tmp_path / "config.yaml")) == (tok, False)              # not regenerated
