"""

Adversarial authentication, transport, privacy, resource, and OAuth regression checks.

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hooks import pideck_hook as hook
from pideck.main import create_app
from pideck.security import Access, COOKIE, MAX_BODY, SecretFilter
from pideck.spotify_auth import authorization, callback_code, exchange_code


# --- Isolated protected application ---


@pytest.fixture
def protected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> TestClient:
    """
    Create an HTTPS application with separate synthetic administrator and hook credentials.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper isolating environment credentials.
        tmp_path (Path): Private temporary configuration directory.

    Returns:
        TestClient: Client without authentication or live source lifecycles.
    """
    monkeypatch.setenv("PIDECK_TOKEN", "display-test-secret")
    monkeypatch.setenv("PIDECK_HOOK_TOKEN", "event-test-secret")
    return TestClient(create_app(str(tmp_path / "config.yaml")), base_url="https://testserver")


# --- Scoped authentication and browser sessions ---


@pytest.mark.parametrize("path", ["/api/slack/clear", "/api/agents/event", "/api/ui-settings"])
def test_authorization_precedes_json_parsing(
    protected: TestClient,
    path: str,
    ) -> None:
    """
    Refuse an unauthenticated request before parsing its malformed JSON body.

    Args:
        protected (TestClient): Isolated protected API client.
        path (str): Mutating operation to exercise.

    Returns:
        None: Malformed JSON cannot bypass the authentication gate.
    """
    assert protected.post(path, content="{", headers={"Content-Type": "application/json"}).status_code == 401


def test_hook_credential_cannot_read_or_control_display(
    protected: TestClient,
    ) -> None:
    """
    Permit hook events without granting Slack access, browser sessions, or controls.

    Args:
        protected (TestClient): Isolated protected API client.

    Returns:
        None: Only event submission is granted to the hook credential.
    """
    headers = {"Authorization": "Bearer event-test-secret"}
    assert protected.post("/api/agents/event", json={"session_id": "s", "event": "SessionStart"}, headers=headers).status_code == 200
    for path in ("/api/slack/clear", "/api/spotify/pause", "/api/session", "/api/ui-settings"):
        assert protected.post(path, json={}, headers=headers).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with protected.websocket_connect("wss://testserver/ws", headers=headers):
            pytest.fail("An event credential must not read the display")


def test_browser_session_is_private_expiring_and_revocable(
    protected: TestClient,
    ) -> None:
    """
    Exchange an administrator credential for a cookie and revoke it on disconnect.

    Args:
        protected (TestClient): Isolated protected API client.

    Returns:
        None: The cookie works for WebSockets and is invalid after logout or expiration.
    """
    response = protected.post("/api/session", headers={"Authorization": "Bearer display-test-secret"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert all(value in cookie for value in ("httponly", "secure", "samesite=strict", "max-age=28800"))
    assert "display-test-secret" not in cookie
    assert protected.get("/api/session").status_code == 401  # A cookie alone is insufficient.
    proof = response.json()["proof"]
    protected.headers["X-piDeck-Session"] = proof
    with protected.websocket_connect("wss://testserver/ws", subprotocols=["pideck-v1", "pideck-proof." + proof]) as ws:
        assert ws.receive_json()["type"] == "snapshot"
    saved = protected.cookies.get(COOKIE)
    assert protected.delete("/api/session").status_code == 200
    assert protected.get("/api/session", headers={"Cookie": f"{COOKIE}={saved}"}).status_code == 401
    protected.app.state.access.sessions[saved] = 0
    assert protected.get("/api/session", headers={"Cookie": f"{COOKIE}={saved}"}).status_code == 401


def test_loopback_is_not_an_authentication_bypass(
    protected: TestClient,
    ) -> None:
    """
    Require credentials from local users and proxies even with a loopback Host header.

    Args:
        protected (TestClient): App carrying synthetic credentials.

    Returns:
        None: Local requests and proxy-shaped requests cannot inherit display access.
    """
    local = TestClient(protected.app, base_url="http://localhost:8765", client=("127.0.0.1", 5000))
    assert local.post("/api/slack/clear").status_code == 401
    assert local.post("/api/slack/clear", headers={"X-Forwarded-For": "192.0.2.10"}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with local.websocket_connect("ws://localhost:8765/ws"):
            pytest.fail("Local users must authenticate")


@pytest.mark.parametrize("origin", ["https://evil.example", "http://testserver", "null"])
def test_foreign_or_downgraded_origin_is_rejected(
    protected: TestClient,
    origin: str,
    ) -> None:
    """
    Refuse cross-site and downgraded browser requests even with valid credentials.

    Args:
        protected (TestClient): Isolated protected API client.
        origin (str): Foreign or invalid browser origin.

    Returns:
        None: The origin check applies to both HTTP and WebSockets.
    """
    headers = {"Authorization": "Bearer display-test-secret", "Origin": origin}
    assert protected.post("/api/slack/clear", headers=headers).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with protected.websocket_connect("wss://testserver/ws", headers=headers):
            pytest.fail("Foreign origins must be rejected")


def test_plaintext_remote_and_query_credentials_are_rejected(
    protected: TestClient,
    ) -> None:
    """
    Prevent plaintext remote authentication and legacy token-bearing WebSocket URLs.

    Args:
        protected (TestClient): Isolated protected API client.

    Returns:
        None: Transport and credential-location restrictions are enforced.
    """
    assert protected.post("http://testserver/api/session", headers={"Authorization": "Bearer display-test-secret"}).status_code == 426
    assert protected.post("/api/session?token=display-test-secret").status_code == 400
    with pytest.raises(WebSocketDisconnect):
        with protected.websocket_connect("wss://testserver/ws?token=display-test-secret"):
            pytest.fail("URL tokens must not authenticate")


# --- Resource and logging bounds ---


def test_large_bodies_and_excessive_attempts_are_refused(
    protected: TestClient,
    ) -> None:
    """
    Enforce raw body bounds and request-attempt limits independently of model fields.

    Args:
        protected (TestClient): Isolated protected API client.

    Returns:
        None: Oversized ignored JSON fields and exhausted request budgets are refused.
    """
    assert protected.post("/api/agents/event", headers={"Authorization": "Bearer event-test-secret"},
                          json={"session_id": "s", "event": "Stop", "ignored": "x" * MAX_BODY}).status_code == 413
    for _ in range(601):
        protected.app.state.access.limited("testclient")
    assert protected.get("/api/health").status_code == 429


def test_tokens_are_redacted_from_server_messages() -> None:
    """
    Remove credentials from accepted and rejected connection log messages.

    Returns:
        None: Query and bearer secrets are absent from the formatted event.
    """
    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 1, "%s Bearer secret-two",
                               ("/ws?token=secret-one&rotate=90",), None)
    assert SecretFilter().filter(record)
    assert "secret-one" not in record.getMessage() and "secret-two" not in record.getMessage()
    assert "rotate=90" in record.getMessage()


@pytest.mark.asyncio
async def test_slow_display_does_not_block_fast_display() -> None:
    """
    Deliver immediately to a fast display while a slow display times out and is removed.

    Returns:
        None: A stalled client cannot indefinitely block updates to other displays.
    """
    from pideck.hub import Hub

    hub = Hub()
    delivered = asyncio.Event()
    slow = Mock()
    fast = Mock()

    async def stall(
        text: str,
        ) -> None:
        """
        Simulate a blocked socket transport.

        Args:
            text (str): Ignored serialized event.

        Returns:
            None: Cancellation ends the deliberately stalled send.
        """
        await asyncio.Event().wait()

    async def accept(
        text: str,
        ) -> None:
        """
        Record delivery to an unaffected display.

        Args:
            text (str): Serialized event received by the fast display.

        Returns:
            None: The delivery event is set.
        """
        delivered.set()

    slow.send_text, fast.send_text = stall, accept
    hub.clients.update((slow, fast))
    task = asyncio.create_task(hub.update("agents", []))
    await asyncio.wait_for(delivered.wait(), 0.5)
    await asyncio.wait_for(task, 3)
    assert slow not in hub.clients and fast in hub.clients


# --- Hook privacy, URLs, and OAuth ---


def test_authenticated_hook_does_not_follow_cross_origin_redirect(
    tmp_path: Path,
    ) -> None:
    """
    Refuse a real HTTP redirect without forwarding the synthetic bearer credential.

    Args:
        tmp_path (Path): Isolated hook settings directory.

    Returns:
        None: The redirected endpoint receives no request or credential.
    """
    received = []

    class Redirect(BaseHTTPRequestHandler):
        """Redirect to a different hostname while recording unexpected follow-up requests."""

        def do_POST(
            self,
            ) -> None:
            """
            Return a cross-origin redirect rather than accepting the event.

            Returns:
                None: The redirect response is sent.
            """
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/unexpected")
            self.end_headers()

        def do_GET(
            self,
            ) -> None:
            """
            Record a forbidden redirect follow-up without displaying its credential.

            Returns:
                None: The unexpected request is recorded.
            """
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(
            self,
            format: str,
            *args: Any,
            ) -> None:
            """
            Suppress synthetic server logs.

            Args:
                format (str): Ignored HTTP log format.
                *args (Any): Ignored HTTP log arguments.

            Returns:
                None: No request details are emitted.
            """
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            hook.post({"session_id": "s", "event": "SessionStart"}, {
                "PIDECK_URL": f"http://127.0.0.1:{server.server_port}", "PIDECK_HOOK_TOKEN": "synthetic",
                "PIDECK_CONFIG": str(tmp_path / "hook.json"),
            })
        assert received == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_upgrade_rotates_legacy_display_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    Invalidate an old all-purpose credential once while preserving unrelated integration settings.

    Args:
        tmp_path (Path): Private temporary configuration directory.
        monkeypatch (pytest.MonkeyPatch): Helper restoring synthetic process credentials.

    Returns:
        None: The migrated credential is fresh and stable across subsequent starts.
    """
    from pideck.__main__ import ensure_token

    monkeypatch.setenv("PIDECK_TOKEN", "legacy-shared-token")
    (tmp_path / ".env").write_text("PIDECK_TOKEN=legacy-shared-token\nOTHER=keep\n")
    value, generated = ensure_token(str(tmp_path / "config.yaml"))
    assert generated and value != "legacy-shared-token" and len(value) >= 32
    assert ensure_token(str(tmp_path / "config.yaml")) == (value, False)
    assert "OTHER=keep" in (tmp_path / ".env").read_text()


@pytest.mark.parametrize("url", ["http://pi.local:8765", "http://192.0.2.1", "file:///tmp/x", "https://user:pass@example.com"])
def test_hook_refuses_unsafe_transport(
    url: str,
    ) -> None:
    """
    Reject plaintext remote destinations and credentials embedded in URLs.

    Args:
        url (str): Unsafe integration destination.

    Returns:
        None: Validation fails before credentials can be sent.
    """
    with pytest.raises(ValueError):
        hook.validate_transport_url(url)


def test_notification_privacy_and_admin_token_separation(
    tmp_path: Path,
    ) -> None:
    """
    Keep notifications private by default and never reuse a display token for hooks.

    Args:
        tmp_path (Path): Location of absent isolated hook preferences.

    Returns:
        None: Explicit opt-in is required and the display token is not inherited.
    """
    env = {"PIDECK_CONFIG": str(tmp_path / "hook.json"), "PIDECK_SEND_MESSAGES": "0", "PIDECK_TOKEN": "admin"}
    event = hook.build_event({"session_id": "s", "hook_event_name": "Notification", "message": "private"}, env)
    assert "message" not in event and hook.load_settings(env)["token"] == ""
    env["PIDECK_SEND_MESSAGES"] = "1"
    assert hook.build_event({"session_id": "s", "hook_event_name": "Notification", "message": "private"}, env)["message"] == "private"


@pytest.mark.parametrize("callback", ["http://127.0.0.1:8888/wrong?code=x&state=expected", "https://evil.example/?code=x&state=expected", "http://127.0.0.1:8888/callback?code=x&state=wrong", "http://127.0.0.1:8888/callback?code=x&state=expected&state=expected"])
def test_spotify_rejects_unrelated_callbacks(
    callback: str,
    ) -> None:
    """
    Refuse wrong callback addresses, stale state, and ambiguous OAuth query parameters.

    Args:
        callback (str): Unrelated or ambiguous callback URI.

    Returns:
        None: No invalid callback code is accepted.
    """
    with pytest.raises(ValueError):
        callback_code(callback, "http://127.0.0.1:8888/callback", "expected")


def test_spotify_pkce_and_exchange_do_not_require_a_shipped_secret() -> None:
    """
    Bind Spotify authorization to PKCE and exchange only the matching verifier.

    Returns:
        None: Authorization includes state and PKCE, and token exchange disables redirects.
    """
    url, state, verifier = authorization("client", "http://127.0.0.1:8888/callback")
    assert "code_challenge_method=S256" in url and state in url and verifier not in url
    code = callback_code(f"http://127.0.0.1:8888/callback?code=one-time&state={state}",
                         "http://127.0.0.1:8888/callback", state)
    response = Mock(status_code=200)
    response.json.return_value = {"refresh_token": "private"}
    with patch("pideck.spotify_auth.httpx.post", return_value=response) as post:
        assert exchange_code(code, "client", "http://127.0.0.1:8888/callback", verifier) == "private"
    assert post.call_args.kwargs["follow_redirects"] is False
    assert post.call_args.kwargs["data"]["code_verifier"] == verifier


def test_private_file_replacement_preserves_other_settings(
    tmp_path: Path,
    ) -> None:
    """
    Persist tokens privately without exposing credentials or discarding unrelated settings.

    Args:
        tmp_path (Path): Isolated configuration directory.

    Returns:
        None: Only the selected credential is replaced in the private configuration.
    """
    from pideck.credentials import SecretStore

    path = tmp_path / ".env"
    path.write_text("OTHER=value\nPIDECK_TOKEN=old\n")
    SecretStore(tmp_path / "config.yaml").set("PIDECK_TOKEN", "new")
    assert "OTHER=value" in path.read_text() and "old" not in path.read_text()
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
