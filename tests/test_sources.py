"""

Spotify parsing, Slack event handling, and lifecycle hook transport tests.

"""

from __future__ import annotations

from typing import Any
from types import ModuleType

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from pideck.hub import Hub
from pideck.sources.slack import SlackFeed, SlackSource, clean_text
from pideck.sources.spotify import Spotify

# --- Tests ---

ROOT = Path(__file__).parent.parent


def test_spotify_parse_track() -> None:
    """
    Verify spotify parse track.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    j = {
        "is_playing": True, "progress_ms": 1000, "currently_playing_type": "track",
        "item": {"id": "x", "name": "Song", "duration_ms": 200000,
                 "artists": [{"name": "A"}, {"name": "B"}],
                 "album": {"name": "Alb", "images": [{"url": "big"}, {"url": "mid"}, {"url": "small"}]}},
    }
    p = Spotify.parse(j)
    assert (p["title"], p["artist"], p["art"], p["is_playing"]) == ("Song", "A, B", "mid", True)


def test_spotify_parse_ad_or_empty() -> None:
    """
    Verify spotify parse ad or empty.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert Spotify.parse({"item": None}) is None
    assert Spotify.parse({"item": {"id": "x"}, "currently_playing_type": "ad"}) is None


def test_slack_clean_text() -> None:
    """
    Verify slack clean text.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    t = "hey <@U123> see <#C1|data-team> <https://example.com/a|the doc> <!here> a &amp; b"
    assert clean_text(t, {"U123": "zack"}) == "hey @zack see #data-team the doc @channel a & b"


def test_slack_events_become_feed_and_alerts() -> None:
    """
    Verify slack events become feed and alerts.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    async def go() -> tuple[list[str], int]:
        """
        Collect feed classifications and alert counts from simulated Slack events.

        Returns:
            tuple[list[str], int]: Published message categories in newest-first order and the number
                of raised alerts.
        """
        hub, sent = Hub(), []

        async def fake_alert(
            *a: Any,
            **k: Any,
            ) -> None:
            """
            Record an alert emitted by the source under test.

            Args:
                *a (Any): Additional arguments accepted by this callback or forwarded to the wrapped
                    operation.
                **k (Any): Additional arguments accepted by this callback or forwarded to the
                    wrapped operation.

            Returns:
                None: Assertions or fixture setup completed successfully.
            """
            sent.append(a)
        hub.alert = fake_alert
        src = SlackSource(SlackFeed(hub, {"alert_on": ["dm", "mention"], "channels": ["C9"]}), {"channels": ["C9"]})

        async def user(
            uid: str,
            ) -> str:
            """
            Return the fixture display name for a Slack user identifier.

            Args:
                uid (str): Slack user identifier to resolve.

            Returns:
                str: The resolved public string or filesystem location described above.
            """
            return "Maya"

        async def chan(
            cid: str,
            ) -> str:
            """
            Return a fixture channel label for a Slack channel identifier.

            Args:
                cid (str): Slack channel identifier to resolve.

            Returns:
                str: The resolved public string or filesystem location described above.
            """
            return "#" + cid
        src._user, src._chan = user, chan
        src._channel = chan
        src._bot_id = "UBOT"
        h = src.handle_event
        await h({"type": "message", "channel_type": "im", "user": "U1", "text": "hi", "ts": "1.0"})
        await h({"type": "app_mention", "user": "U1", "channel": "C1", "text": "yo", "ts": "2.0"})
        await h({"type": "message", "channel": "C9", "user": "U1", "text": "chat", "ts": "3.0"})
        await h({"type": "message", "channel": "C5", "user": "U1", "text": "unwatched", "ts": "4.0"})
        await h({"type": "message", "channel_type": "im", "user": "UBOT", "text": "me", "ts": "5.0"})
        await h({"type": "message", "channel_type": "im", "user": "U1", "subtype": "message_changed"})
        kinds = [m["kind"] for m in hub.state["slack"]]
        return kinds, len(sent)

    kinds, alerts = asyncio.run(go())
    assert kinds == ["channel", "mention", "dm"]  # newest first; unwatched/self/edits ignored
    assert alerts == 2                            # dm + mention, not plain channel chatter


def test_hook_script_roundtrip(
    tmp_path: Path,
    ) -> None:
    """
    Verify hook script roundtrip.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    tr = tmp_path / "t.jsonl"
    tr.write_text(
        json.dumps({"type": "user"}) + "\n"
        + json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 49990, "cache_creation_input_tokens": 0}}}) + "\n"
    )
    sys.path.insert(0, str(ROOT / "hooks"))
    import pideck_hook

    ev = pideck_hook.build_event(
        {"session_id": "s", "hook_event_name": "Stop", "cwd": "/x/proj", "transcript_path": str(tr)}, {}
    )
    assert ev["context_pct"] == 25.0 and ev["event"] == "Stop"
    # SessionEnd skips the transcript read; message is truncated; name override works
    ev = pideck_hook.build_event({"session_id": "s", "hook_event_name": "SessionEnd", "transcript_path": str(tr),
                                  "message": "m" * 500}, {"PIDECK_NAME": "mine"})
    assert "context_pct" not in ev and ev["name"] == "mine" and len(ev["message"]) == 200


def test_hook_never_fails_when_server_down() -> None:
    """
    Verify hook never fails when server down.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py")],
                       input='{"session_id":"s","hook_event_name":"Stop"}', text=True,
                       env={"PIDECK_URL": "http://127.0.0.1:1", "PATH": ""}, capture_output=True)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py")], input="not json", text=True,
                       capture_output=True)
    assert r.returncode == 0


def _hook() -> ModuleType:
    """
    Import the standalone hook module from the checkout for direct behavior tests.

    Returns:
        ModuleType: Imported standalone hook module for direct behavior assertions.
    """
    sys.path.insert(0, str(ROOT / "hooks"))
    import pideck_hook
    return pideck_hook


def test_hook_source_flag_and_codex_payload() -> None:
    """
    Verify hook source flag and codex payload.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    h = _hook()
    assert h.parse_args([]) == ("claude", None)
    assert h.parse_args(["--source", "codex"]) == ("codex", None)
    ev = h.build_event(
        {"session_id": "t1", "hook_event_name": "Stop", "cwd": "/w/p", "transcript_path": "/nope",
         "last_assistant_message": "Done.\n\nTests   pass."}, {"PIDECK_SEND_MESSAGES": "1"}, "codex")
    assert ev["agent"] == "codex" and ev["event"] == "Stop"
    assert ev["message"] == "Done. Tests pass."   # whitespace collapsed
    assert "context_pct" not in ev                # ctx% is Claude-only


def test_hook_codex_notify_mode() -> None:
    """
    Verify hook codex notify mode.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    h = _hook()
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "th1", "turn-id": "9",
                          "cwd": "/w/proj", "last-assistant-message": "Renamed foo."})
    source, notify = h.parse_args([payload])          # Codex passes the JSON as the last argument
    assert source == "codex" and notify == payload
    ev = h.build_notify_event(json.loads(payload), {"PIDECK_SEND_MESSAGES": "1"})
    assert (ev["session_id"], ev["event"], ev["agent"], ev["message"]) == ("th1", "Stop", "codex", "Renamed foo.")
    assert h.build_notify_event({"type": "something-new"}, {}) is None


def test_hook_prints_nothing(
    tmp_path: Path,
    ) -> None:
    # Codex treats stray stdout from a Stop hook as a failure
    """
    Verify hook prints nothing.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py"), "--source", "codex"],
                       input='{"session_id":"s","hook_event_name":"Stop"}', text=True,
                       env={"PIDECK_URL": "http://127.0.0.1:1", "PATH": ""}, capture_output=True)
    assert r.returncode == 0 and r.stdout == ""


def test_assistant_reply_snippets_are_opt_in(
    tmp_path: Path,
    ) -> None:
    """
    Verify assistant reply snippets are opt in.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    h = _hook()
    payload = {"session_id": "t", "hook_event_name": "Stop", "last_assistant_message": "secret plans"}
    no_conf = {"PIDECK_CONFIG": str(tmp_path / "none.json")}
    assert "message" not in h.build_event(payload, no_conf, "codex")           # default: not sent
    assert "message" not in h.build_notify_event(
        {"type": "agent-turn-complete", "last-assistant-message": "x"}, no_conf)
    saved = tmp_path / "hook.json"
    saved.write_text('{"send_messages": true}')
    assert h.build_event(payload, {"PIDECK_CONFIG": str(saved)}, "codex")["message"] == "secret plans"
    assert "message" not in h.build_event(payload, {"PIDECK_CONFIG": str(saved), "PIDECK_SEND_MESSAGES": "0"}, "codex")
    # Notification text is short and written by the tool itself, so it is always sent
    assert h.build_event({"session_id": "t", "hook_event_name": "Notification", "message": "needs you"}, no_conf)["message"] == "needs you"
