"""Startup discovery of local sessions and Cursor allowance parsing."""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import pideck_hook as hook


"""# --- Local session discovery ---"""


def test_discover_sessions_uses_project_names_and_skips_old_files(
        tmp_path,
):
    """Recent Claude, Codex, and Cursor files become tiles. Their text does not."""
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
        tmp_path,
):
    """The local sign-in is used for the request and is absent from the event."""
    db = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("create table ItemTable (key text primary key, value text)")
    conn.execute("insert into ItemTable values (?, ?)", ("cursorAuth/accessToken", "secret-token"))
    conn.commit()
    conn.close()
    seen = {}

    def opener(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout

        class Response:
            def read(self):
                return json.dumps({
                    "planUsage": {"totalPercentUsed": 40, "apiPercentUsed": 5},
                    "billingCycleEnd": 2_000_000_000,
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()

    event = hook.fetch_cursor_usage({"CURSOR_STATE_DB": str(db)}, opener)
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


def test_cached_claude_usage_is_reposted(
        tmp_path,
):
    """A status-line sample survives into the next launch as percentages only."""
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
