import asyncio
import json
import subprocess
import sys
from pathlib import Path

from pideck.hub import Hub
from pideck.sources.slack import SlackFeed, SlackSource, clean_text
from pideck.sources.spotify import Spotify

ROOT = Path(__file__).parent.parent


def test_spotify_parse_track():
    j = {
        "is_playing": True, "progress_ms": 1000, "currently_playing_type": "track",
        "item": {"id": "x", "name": "Song", "duration_ms": 200000,
                 "artists": [{"name": "A"}, {"name": "B"}],
                 "album": {"name": "Alb", "images": [{"url": "big"}, {"url": "mid"}, {"url": "small"}]}},
    }
    p = Spotify.parse(j)
    assert (p["title"], p["artist"], p["art"], p["is_playing"]) == ("Song", "A, B", "mid", True)


def test_spotify_parse_ad_or_empty():
    assert Spotify.parse({"item": None}) is None
    assert Spotify.parse({"item": {"id": "x"}, "currently_playing_type": "ad"}) is None


def test_slack_clean_text():
    t = "hey <@U123> see <#C1|data-team> <https://example.com/a|the doc> <!here> a &amp; b"
    assert clean_text(t, {"U123": "zack"}) == "hey @zack see #data-team the doc @channel a & b"


def test_slack_events_become_feed_and_alerts():
    async def go():
        hub, sent = Hub(), []

        async def fake_alert(*a, **k):
            sent.append(a)
        hub.alert = fake_alert
        src = SlackSource(SlackFeed(hub, {"alert_on": ["dm", "mention"], "channels": ["C9"]}), {"channels": ["C9"]})

        async def user(uid):
            return "Maya"

        async def chan(cid):
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


def test_hook_script_roundtrip(tmp_path):
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


def test_hook_never_fails_when_server_down():
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py")],
                       input='{"session_id":"s","hook_event_name":"Stop"}', text=True,
                       env={"PIDECK_URL": "http://127.0.0.1:1", "PATH": ""}, capture_output=True)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py")], input="not json", text=True,
                       capture_output=True)
    assert r.returncode == 0


def _hook():
    sys.path.insert(0, str(ROOT / "hooks"))
    import pideck_hook
    return pideck_hook


def test_hook_source_flag_and_codex_payload():
    h = _hook()
    assert h.parse_args([]) == ("claude", None)
    assert h.parse_args(["--source", "codex"]) == ("codex", None)
    ev = h.build_event(
        {"session_id": "t1", "hook_event_name": "Stop", "cwd": "/w/p", "transcript_path": "/nope",
         "last_assistant_message": "Done.\n\nTests   pass."}, {}, "codex")
    assert ev["agent"] == "codex" and ev["event"] == "Stop"
    assert ev["message"] == "Done. Tests pass."   # whitespace collapsed
    assert "context_pct" not in ev                # ctx% is Claude-only


def test_hook_codex_notify_mode():
    h = _hook()
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "th1", "turn-id": "9",
                          "cwd": "/w/proj", "last-assistant-message": "Renamed foo."})
    source, notify = h.parse_args([payload])          # Codex passes the JSON as the last argument
    assert source == "codex" and notify == payload
    ev = h.build_notify_event(json.loads(payload), {})
    assert (ev["session_id"], ev["event"], ev["agent"], ev["message"]) == ("th1", "Stop", "codex", "Renamed foo.")
    assert h.build_notify_event({"type": "something-new"}, {}) is None


def test_hook_prints_nothing(tmp_path):
    # Codex treats stray stdout from a Stop hook as a failure
    r = subprocess.run([sys.executable, str(ROOT / "hooks/pideck_hook.py"), "--source", "codex"],
                       input='{"session_id":"s","hook_event_name":"Stop"}', text=True,
                       env={"PIDECK_URL": "http://127.0.0.1:1", "PATH": ""}, capture_output=True)
    assert r.returncode == 0 and r.stdout == ""
