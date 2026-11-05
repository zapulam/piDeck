"""

Enforce scoped credentials, private browser sessions, request limits, and log redaction.

"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import hmac
import logging
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


# --- Authentication and browser sessions ---

COOKIE = "pideck_session"
SESSION_SECONDS = 8 * 3600
MAX_BODY = 16 * 1024
MAX_SESSIONS = 128


class Access:
    """Authenticate display administrators and event-only agent integrations."""

    def __init__(
        self,
        demo: bool = False,
        ) -> None:
        """
        Capture server credentials and create a bounded browser-session registry.

        Args:
            demo (bool): Whether explicit mock mode permits loopback access to synthetic data.

        Returns:
            None: The access policy is ready for request checks.
        """
        self.admin = os.environ.get("PIDECK_TOKEN", "")
        self.hook = os.environ.get("PIDECK_HOOK_TOKEN", "")
        if self.admin and self.hook and hmac.compare_digest(self.admin.encode(), self.hook.encode()):
            raise ValueError("Display and hook credentials must be different")
        self.demo = demo
        self.activation = ""
        self.bootstrap: dict[str, float] = {}
        self.sessions: OrderedDict[str, float] = OrderedDict()
        self.proofs: dict[str, str] = {}
        self.attempts: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def authorized(
        self,
        conn: HTTPConnection,
        ) -> int:
        """
        Validate transport, browser origin, and credentials for the requested operation.

        Args:
            conn (HTTPConnection): HTTP or WebSocket connection to authorize.

        Returns:
            int: Zero when permitted, otherwise the HTTP rejection status.
        """
        host = conn.headers.get("host", "").lower()
        try:
            hostname = urlsplit("//" + host).hostname
            origin = urlsplit(conn.headers.get("origin", ""))
        except ValueError:
            return 403
        local = bool(conn.client and conn.client.host in {"127.0.0.1", "::1"}
                     and hostname in {"localhost", "127.0.0.1", "::1"})
        if conn.url.scheme not in {"https", "wss"} and not local:
            return 426
        if conn.headers.get("origin"):
            expected = "https" if conn.url.scheme in {"https", "wss"} else "http"
            if origin.scheme != expected or origin.netloc.lower() != host:
                return 403
        if conn.query_params.get("token") is not None:
            return 400
        authorization = conn.headers.get("authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                return 401
            given = authorization[7:].encode()
            if self.bootstrap.get(authorization[7:], 0) > time.monotonic():
                return 0 if conn.url.path == "/api/session" and conn.scope.get("method") == "POST" else 403
            if self.activation and hmac.compare_digest(given, self.activation.encode()):
                return 0 if conn.url.path == "/api/desktop/activate" and conn.scope.get("method") == "POST" else 403
            if self.admin and hmac.compare_digest(given, self.admin.encode()):
                return 0
            if self.hook and hmac.compare_digest(given, self.hook.encode()):
                return 0 if conn.url.path == "/api/agents/event" and conn.scope.get("method") == "POST" else 403
            return 401
        session = conn.cookies.get(COOKIE, "")
        proof = conn.headers.get("x-pideck-session", "")
        if conn.scope["type"] == "websocket":
            proof = next((item.removeprefix("pideck-proof.") for item in conn.scope.get("subprotocols", [])
                          if item.startswith("pideck-proof.")), "")
        if (self.sessions.get(session, 0) > time.monotonic() and proof
                and hmac.compare_digest(proof, self.proofs.get(session, ""))):
            return 0
        return 0 if self.demo and local else 401

    def session(
        self,
        ) -> str:
        """
        Issue an unpredictable browser credential with a fixed lifetime.

        Returns:
            str: New opaque session value; the oldest entry is evicted when full.
        """
        value = secrets.token_urlsafe(32)
        self.sessions[value] = time.monotonic() + SESSION_SECONDS
        self.proofs[value] = secrets.token_urlsafe(32)
        while len(self.sessions) > MAX_SESSIONS:
            expired, _ = self.sessions.popitem(last=False)
            self.proofs.pop(expired, None)
        return value

    def limited(
        self,
        peer: str,
        ) -> bool:
        """
        Bound API and WebSocket attempts per peer with bounded bookkeeping.

        Args:
            peer (str): Actual or explicitly trusted-proxy-derived client address.

        Returns:
            bool: True after 600 attempts in the current minute.
        """
        now = time.monotonic()
        began, count = self.attempts.pop(peer, (now, 0))
        if now - began >= 60:
            began, count = now, 0
        self.attempts[peer] = (began, count + 1)
        while len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        return count >= 600


# --- Authentication before body parsing ---


class RequestGuard:
    """Reject unauthorized or oversized API requests before JSON parsing."""

    def __init__(
        self,
        app: ASGIApp,
        access: Access,
        ) -> None:
        """
        Attach request bounds to the configured application's access policy.

        Args:
            app (ASGIApp): Next ASGI handler.
            access (Access): Application-owned authentication and rate-limit state.

        Returns:
            None: The middleware is initialized.
        """
        self.app, self.access = app, access

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        ) -> None:
        """
        Check API access and read at most sixteen KiB before invoking the router.

        Args:
            scope (Scope): Incoming ASGI connection metadata.
            receive (Receive): Client request event receiver.
            send (Send): Response event sender.

        Returns:
            None: The rejection response or downstream response is sent.
        """
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        peer = conn.client.host if conn.client else "unknown"
        public = scope["path"] in {"/api/health", "/api/config"} and scope["method"] == "GET"
        status = 429 if self.access.limited(peer) else (0 if public else self.access.authorized(conn))
        if status:
            return await JSONResponse({"detail": "access refused"}, status)(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > MAX_BODY:
                        return await JSONResponse({"detail": "request too large"}, 413)(scope, receive, send)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "request timed out"}, 408)(scope, receive, send)
        delivered = False

        async def replay() -> dict:
            """
            Replay the bounded body once, then retain the original disconnect receiver.

            Returns:
                dict: ASGI request or disconnect event.
            """
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


# --- Log redaction ---


class SecretFilter(logging.Filter):
    """Remove URL authentication parameters and bearer values from service logs."""

    def filter(
        self,
        record: logging.LogRecord,
        ) -> bool:
        """
        Format and redact a logging event before its handlers persist it.

        Args:
            record (logging.LogRecord): Event whose formatted text may contain URL credentials.

        Returns:
            bool: True so the sanitized event is retained.
        """
        def redact(
            value: object,
            ) -> object:
            """
            Redact strings while retaining structured logging arguments for access formatters.

            Args:
                value (object): Message or formatting argument to sanitize.

            Returns:
                object: Sanitized string, or the unchanged non-string value.
            """
            if not isinstance(value, str):
                return value
            value = re.sub(r"(?i)([#?&](?:token|access_token|refresh_token|code)=)[^&\s\"\]]+",
                           r"\1[REDACTED]", value)
            value = re.sub(r"pideck-proof\.[A-Za-z0-9_-]+", "pideck-proof.[REDACTED]", value)
            value = re.sub(r"(?i)((?:x-pideck-session|pideck_session|access_token|refresh_token|client_secret|PIDECK_TOKEN|PIDECK_HOOK_TOKEN)['\"\s:=]+)[^'\"\s,;}\]]+",
                           r"\1[REDACTED]", value)
            return re.sub(r"(?i)(Bearer|Basic)\s+(?!%s)[^\s\"']+", r"\1 [REDACTED]", value)

        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            record.args = tuple(redact(value) for value in record.args)
        else:
            record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = str(redact(logging.Formatter().formatException(record.exc_info)))
            record.exc_info = None
        return True


def redact_logs() -> None:
    """
    Redact HTTP and WebSocket URLs even when rejected legacy clients send query tokens.

    Returns:
        None: Application and server loggers filter secret-bearing messages.
    """
    for name in ("pideck", "uvicorn.error", "uvicorn.access", "pideck.desktop", "pywebview"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SecretFilter) for item in logger.filters):
            logger.addFilter(SecretFilter())
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, SecretFilter) for item in handler.filters):
            handler.addFilter(SecretFilter())
