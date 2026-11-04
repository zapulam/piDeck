"""

Who is allowed to read from or control the display.

"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import httpx

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pideck.main import create_app
from pideck.sources.agents import MAX_SESSIONS

# --- Tests ---

BODY = {"session_id": "s", "event": "SessionStart"}


def make(
    monkeypatch: pytest.MonkeyPatch,
    token: str = 'sekret',
    **kw: Any,
    ) -> TestClient:
    """
    Create a security test client without relying on a platform-specific working directory.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper for temporary authentication environment
            changes.
        token (str): Shared test token, or an empty string to test an unconfigured server.
        **kw (Any): Additional arguments for FastAPI's TestClient.

    Returns:
        TestClient: HTTP client configured with the requested test origin and authentication
            settings.
    """
    if token:
        monkeypatch.setenv("PIDECK_TOKEN", token)
    else:
        monkeypatch.delenv("PIDECK_TOKEN", raising=False)
    return TestClient(create_app("/nonexistent/config.yaml"), **kw)


def kiosk(
    monkeypatch: pytest.MonkeyPatch,
    token: str = 'sekret',
    ) -> TestClient:
    """
    A request from the Pi's own browser: loopback peer, Host: localhost, no token.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        token (str): Access token used only for the intended local or provider request.

    Returns:
        TestClient: Client using a loopback peer and localhost Host header to simulate the Pi
            browser.
    """
    return make(monkeypatch, token, base_url="http://localhost:8765", client=("127.0.0.1", 50000))


# TestClient gives websockets "Host: testserver", so say what a real browser would send.
KIOSK_WS = {"Host": "localhost:8765"}

ENDPOINTS = [("post", "/api/agents/event", BODY), ("delete", "/api/agents/s", None),
             ("post", "/api/spotify/pause", None), ("post", "/api/slack/clear", None)]


def call(
    c: TestClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    **kw: Any,
    ) -> httpx.Response:
    """
    Send a parameterized test request with optional JSON content and request options.

    Args:
        c (TestClient): Socket client or HTTP test client used by the operation.
        method (str): HTTP method to use for the request.
        path (str): Filesystem path or API path used by this operation.
        body (dict[str, Any] | None): Validated request body or optional JSON fields to send.
        **kw (Any): Additional arguments accepted by this callback or forwarded to the wrapped
            operation.

    Returns:
        httpx.Response: HTTP response from the parameterized security-test request.
    """
    return getattr(c, method)(path, **({"json": body} if body else {}), **kw)


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_remote_needs_the_token(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    ) -> None:
    """
    Verify remote needs the token.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.
        method (str): HTTP method to use for the request.
        path (str): Filesystem path or API path used by this operation.
        body (dict[str, Any] | None): Validated request body or optional JSON fields to send.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch) as c:
        assert call(c, method, path, body).status_code == 401
        assert call(c, method, path, body, headers={"Authorization": "Bearer nope"}).status_code == 401
        assert call(c, method, path, body, headers={"Authorization": "Bearer sekret"}).status_code in (200, 409)


def test_websocket_needs_the_token_from_other_machines(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify websocket needs the token from other machines.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws?token=nope"):
                pass
        with c.websocket_connect("/ws?token=sekret") as ws:      # how a phone or laptop browser connects
            assert ws.receive_json()["type"] == "snapshot"


def test_kiosk_on_this_machine_needs_no_token(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify kiosk on this machine needs no token.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with kiosk(monkeypatch) as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 200
        with c.websocket_connect("/ws", headers=KIOSK_WS) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        assert c.post("/api/slack/clear").status_code == 200


def test_wrong_token_is_refused_even_from_this_machine(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify wrong token is refused even from this machine.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with kiosk(monkeypatch) as c:
        assert c.post("/api/agents/event", json=BODY, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_dns_rebinding_host_is_not_treated_as_this_machine(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify dns rebinding host is not treated as this machine.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch, base_url="http://evil.example:8765", client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/slack/clear").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"Host": "evil.example:8765"}):
                pass


def test_cross_site_requests_are_refused_even_from_this_machine(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify cross site requests are refused even from this machine.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
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


def test_no_token_configured_means_only_this_machine(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify no token configured means only this machine.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch, token="") as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 401
        assert c.post("/api/agents/event", json=BODY, headers={"Authorization": "Bearer "}).status_code == 401
    with kiosk(monkeypatch, token="") as c:
        assert c.post("/api/agents/event", json=BODY).status_code == 200


def test_security_headers_on_every_response(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify security headers on every response.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch) as c:
        for path in ("/", "/api/health", "/app.js"):
            h = c.get(path).headers
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_oversized_and_out_of_range_fields_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify oversized and out of range fields are rejected.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch) as c:
        auth = {"Authorization": "Bearer sekret"}
        for bad in ({"name": "x" * 65}, {"message": "x" * 501}, {"cwd": "x" * 513},
                    {"context_pct": 150}, {"context_pct": -1}, {"session_id": "x" * 129}):
            body = {"session_id": "s", "event": "Stop", **bad}
            assert c.post("/api/agents/event", json=body, headers=auth).status_code == 422, bad


def test_session_count_is_capped(
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify session count is capped.

    Args:
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with make(monkeypatch) as c:
        auth = {"Authorization": "Bearer sekret"}
        for i in range(MAX_SESSIONS + 5):
            c.post("/api/agents/event", json={"session_id": f"s{i}", "event": "UserPromptSubmit"}, headers=auth)
        assert len(c.app.state.agents.agents) == MAX_SESSIONS


def test_first_run_creates_and_saves_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Verify first run creates and saves a token.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
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
