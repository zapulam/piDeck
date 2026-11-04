"""

Startup discovery of local sessions and Cursor allowance parsing.

"""

from __future__ import annotations

from typing import Any, Self

import json
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import pideck_hook as hook


# --- Local session discovery ---


def test_discover_sessions_uses_project_names_and_skips_old_files(
    tmp_path: Path,
    ) -> None:
    """
    Recent Claude, Codex, and Cursor files become tiles. Their text does not.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    now = time.time()
    claude = tmp_path / ".claude" / "projects" / "C--work-demo"
    claude.mkdir(parents=True)
    (claude / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "cwd": "C:/work/demo", "message": "private prompt"}) + "\n",
        encoding="utf-8",
    )
    old = claude / "11111111-2222-3333-4444-555555555555.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    old_time = now - 10 * 3600
    import os
    os.utime(old, (old_time, old_time))
    codex = tmp_path / ".codex" / "sessions" / "03"
    codex.mkdir(parents=True)
    (codex / "rollout-2026-01-01T00-00-00-01999999-aaaa-7bbb-8ccc-ddddeeeeffff.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {
            "session_id": "01999999-aaaa-7bbb-8ccc-ddddeeeeffff", "cwd": "/work/site",
            "base_instructions": "private instructions",
        }}) + "\n",
        encoding="utf-8",
    )
    cursor = tmp_path / ".cursor" / "projects" / "c-work-piDeck" / "agent-transcripts" / "abc12345-1111-2222-3333-444455556666"
    cursor.mkdir(parents=True)
    (cursor / "abc12345-1111-2222-3333-444455556666.jsonl").write_text(
        json.dumps({"role": "user", "message": {"content": [{"text": "private"}]}}) + "\n",
        encoding="utf-8",
    )
    events = hook.discover_sessions(tmp_path, now, 6 * 3600)
    dumped = json.dumps(events)
    assert "private" not in dumped
    by_agent = {ev["agent"]: ev for ev in events}
    assert by_agent["claude"]["name"] == "demo" and by_agent["claude"]["state"] == "working"
    assert by_agent["codex"]["session_id"] == "01999999-aaaa-7bbb-8ccc-ddddeeeeffff"
    assert by_agent["codex"]["name"] == "site"
    assert by_agent["cursor"]["name"] == "piDeck"
    assert by_agent["cursor"]["session_id"] == "cursor:abc12345-1111-2222-3333-444455556666"
    assert len(events) == 3


def test_cursor_usage_posts_percents_and_not_the_token(
    tmp_path: Path,
    ) -> None:
    """
    The local sign-in is used for the request and is absent from the event.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("create table ItemTable (key text primary key, value text)")
    conn.execute("insert into ItemTable values (?, ?)", ("cursorAuth/accessToken", "secret-token"))
    conn.commit()
    conn.close()
    seen = {}

    def opener(
        request: urllib.request.Request,
        timeout: float,
        ) -> Any:
        """
        Return a simulated provider response and record the outgoing request.

        Args:
            request (urllib.request.Request): Incoming request or outgoing provider request used by
                this operation.
            timeout (float): Maximum duration allowed for the simulated HTTP operation.

        Returns:
            Any: The provider value or response object described above.
        """
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout

        class Response:
            def read(
                self,
                ) -> bytes:
                """
                Encode the simulated provider response as JSON bytes.

                Returns:
                    bytes: Encoded response or image bytes produced by this operation.
                """
                return json.dumps({
                    "planUsage": {"totalPercentUsed": 40, "apiPercentUsed": 5},
                    "billingCycleEnd": 2_000_000_000,
                }).encode()

            def __enter__(
                self,
                ) -> Self:
                """
                Enter the simulated HTTP response context.

                Returns:
                    Self: This context-managed response instance.
                """
                return self

            def __exit__(
                self,
                *args: Any,
                ) -> bool:
                """
                Leave the simulated HTTP response context without suppressing exceptions.

                Args:
                    *args (Any): Additional arguments accepted by this callback or forwarded to the
                        wrapped operation.

                Returns:
                    bool: True when the documented condition holds; otherwise False.
                """
                return False

        return Response()

    event = hook.fetch_cursor_usage(
        {"CURSOR_STATE_DB": str(db), "PIDECK_CONFIG": str(tmp_path / "hook.json")},
        opener,
    )
    assert seen == {"auth": "Bearer secret-token", "timeout": 5}
    assert event["agent"] == "cursor" and event["event"] == "UsageUpdate"
    assert event["usage"]["primary"] == {
        "used_pct": 40, "window_minutes": 43200, "resets_at": 2_000_000_000, "label": "Included",
    }
    assert event["usage"]["secondary"]["label"] == "API"
    assert "secret-token" not in json.dumps(event)
    dashboard = hook.cursor_usage_from_body({
        "billingCycleEnd": "1789718953000",
        "planUsage": {"includedSpend": 1516, "limit": 2000, "apiPercentUsed": 0.7, "totalPercentUsed": 3},
    })
    assert round(dashboard["primary"]["used_pct"], 1) == 75.8
    assert dashboard["secondary"]["used_pct"] == 0.7
    snake = hook.cursor_usage_from_body({
        "billing_cycle_end": 2_000_000_000,
        "individual_usage": {"plan": {"included_spend": 20, "limit": 100, "api_percent_used": 4}},
    })
    assert snake["primary"]["used_pct"] == 20
    assert snake["secondary"]["label"] == "API"


def test_cursor_usage_refreshes_an_expired_access_token(
    tmp_path: Path,
    ) -> None:
    """
    A rejected access token is refreshed once, and neither token is posted.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("create table ItemTable (key text primary key, value text)")
    conn.execute("insert into ItemTable values (?, ?)", ("cursorAuth/accessToken", "secret-token"))
    conn.execute("insert into ItemTable values (?, ?)", ("cursorAuth/refreshToken", "refresh-secret"))
    conn.commit()
    conn.close()
    calls = []

    def opener(
        request: urllib.request.Request,
        timeout: float,
        ) -> Any:
        """
        Return a simulated provider response and record the outgoing request.

        Args:
            request (urllib.request.Request): Incoming request or outgoing provider request used by
                this operation.
            timeout (float): Maximum duration allowed for the simulated HTTP operation.

        Returns:
            Any: The provider value or response object described above.
        """
        calls.append(request.full_url)
        assert timeout == 5
        if request.full_url.endswith("/oauth/token"):

            class Refresh:
                def read(
                    self,
                    ) -> bytes:
                    """
                    Encode the simulated provider response as JSON bytes.

                    Returns:
                        bytes: Encoded response or image bytes produced by this operation.
                    """
                    return json.dumps({"access_token": "fresh-token", "shouldLogout": False}).encode()

                def __enter__(
                    self,
                    ) -> Self:
                    """
                    Enter the simulated HTTP response context.

                    Returns:
                        Self: This context-managed response instance.
                    """
                    return self

                def __exit__(
                    self,
                    *args: Any,
                    ) -> bool:
                    """
                    Leave the simulated HTTP response context without suppressing exceptions.

                    Args:
                        *args (Any): Additional arguments accepted by this callback or forwarded to
                            the wrapped operation.

                    Returns:
                        bool: True when the documented condition holds; otherwise False.
                    """
                    return False

            return Refresh()
        if calls.count(request.full_url) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "expired", None, None)

        class Response:
            def read(
                self,
                ) -> bytes:
                """
                Encode the simulated provider response as JSON bytes.

                Returns:
                    bytes: Encoded response or image bytes produced by this operation.
                """
                return json.dumps({
                    "planUsage": {"includedSpend": 10, "limit": 100, "apiPercentUsed": 2},
                    "billingCycleEnd": 2_000_000_000,
                }).encode()

            def __enter__(
                self,
                ) -> Self:
                """
                Enter the simulated HTTP response context.

                Returns:
                    Self: This context-managed response instance.
                """
                return self

            def __exit__(
                self,
                *args: Any,
                ) -> bool:
                """
                Leave the simulated HTTP response context without suppressing exceptions.

                Args:
                    *args (Any): Additional arguments accepted by this callback or forwarded to the
                        wrapped operation.

                Returns:
                    bool: True when the documented condition holds; otherwise False.
                """
                return False

        return Response()

    event = hook.fetch_cursor_usage(
        {"CURSOR_STATE_DB": str(db), "PIDECK_CONFIG": str(tmp_path / "hook.json")},
        opener,
    )
    assert [path.split("cursor.sh", 1)[-1] for path in calls] == [
        "/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        "/oauth/token",
        "/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
    ]
    assert event["usage"]["primary"]["used_pct"] == 10
    dumped = json.dumps(event) + (tmp_path / "usage-cache.json").read_text(encoding="utf-8")
    assert "secret-token" not in dumped
    assert "refresh-secret" not in dumped
    assert "fresh-token" not in dumped


def test_cached_claude_usage_is_reposted(
    tmp_path: Path,
    ) -> None:
    """
    A status-line sample survives into the next launch as percentages only.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    env = {"PIDECK_CONFIG": str(tmp_path / "hook.json")}
    hook.remember_usage({
        "agent": "claude",
        "usage": {"primary": {"used_pct": 12, "window_minutes": 300, "resets_at": None}},
        "message": "private",
    }, env)
    events = hook.cached_usage_events(env)
    assert events == [{
        "session_id": "usage:claude",
        "event": "UsageUpdate",
        "agent": "claude",
        "usage": {"primary": {"used_pct": 12, "window_minutes": 300, "resets_at": None}},
    }]
    assert "private" not in (tmp_path / "usage-cache.json").read_text(encoding="utf-8")


def test_claude_usage_posts_percents_and_backs_off(
    tmp_path: Path,
    ) -> None:
    """
    The local Claude sign-in fills 5h and weekly bars, and a recent sample is not asked again.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret-token"}}), encoding="utf-8")
    env = {"CLAUDE_CREDENTIALS": str(creds), "PIDECK_CONFIG": str(tmp_path / "hook.json")}
    calls = []

    def opener(
        request: urllib.request.Request,
        timeout: float,
        ) -> Any:
        """
        Return a simulated provider response and record the outgoing request.

        Args:
            request (urllib.request.Request): Incoming request or outgoing provider request used by
                this operation.
            timeout (float): Maximum duration allowed for the simulated HTTP operation.

        Returns:
            Any: The provider value or response object described above.
        """
        calls.append(request.get_header("Authorization"))
        assert request.get_header("Anthropic-beta") == "oauth-2025-04-20"
        assert timeout == 5

        class Response:
            def read(
                self,
                ) -> bytes:
                """
                Encode the simulated provider response as JSON bytes.

                Returns:
                    bytes: Encoded response or image bytes produced by this operation.
                """
                return json.dumps({
                    "five_hour": {"utilization": 22, "resets_at": "2026-11-04T12:00:00Z"},
                    "seven_day": {"utilization": 41, "resets_at": "2026-11-08T12:00:00Z"},
                }).encode()

            def __enter__(
                self,
                ) -> Self:
                """
                Enter the simulated HTTP response context.

                Returns:
                    Self: This context-managed response instance.
                """
                return self

            def __exit__(
                self,
                *args: Any,
                ) -> bool:
                """
                Leave the simulated HTTP response context without suppressing exceptions.

                Args:
                    *args (Any): Additional arguments accepted by this callback or forwarded to the
                        wrapped operation.

                Returns:
                    bool: True when the documented condition holds; otherwise False.
                """
                return False

        return Response()

    event = hook.fetch_claude_usage(env, opener, now=1_000_000)
    assert calls == ["Bearer secret-token"]
    assert event["usage"]["primary"]["label"] == "5h"
    assert event["usage"]["primary"]["used_pct"] == 22
    assert event["usage"]["secondary"]["label"] == "Weekly"
    assert event["usage"]["secondary"]["used_pct"] == 41
    dumped = json.dumps(event) + (tmp_path / "usage-cache.json").read_text(encoding="utf-8")
    assert "secret-token" not in dumped
    assert hook.fetch_claude_usage(env, opener, now=1_000_100) is None
    assert calls == ["Bearer secret-token"]
