"""Account usage transport, provider parsing, and status-line installation regressions."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import install
import pideck_hook as hook


"""# --- Registry integration ---"""


def test_usage_update_preserves_session_state_and_snapshot(
        client):
    """Usage updates must not wake a waiting agent, reset its age, or emit alerts."""
    client.post("/api/agents/event", json={"session_id": "s", "state": "waiting", "agent": "claude"})
    before = dict(client.app.state.agents.agents["s"])
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        response = client.post("/api/agents/event", json={
            "session_id": "s", "event": "UsageUpdate", "agent": "claude", "context_pct": 42,
            "usage": {"primary": {"used_pct": 0, "window_minutes": 300, "resets_at": 2000000000}},
        })
        assert response.status_code == 200
        assert ws.receive_json()["key"] == "agent_usage"
        agent = ws.receive_json()["value"][0]
        assert agent == {**before, "context_pct": 42}
        # An unknown/ended session must not be recreated by a late status-line callback.
        client.post("/api/agents/event", json={"session_id": "late", "event": "UsageUpdate", "agent": "codex",
                                              "usage": {"secondary": {"used_pct": 89, "window_minutes": 10080}}})
        assert ws.receive_json()["key"] == "agent_usage"
    with client.websocket_connect("/ws") as ws:
        state = ws.receive_json()["state"]
        assert len(state["agents"]) == 1
        assert state["agent_usage"]["claude"]["primary"]["used_pct"] == 0
        assert state["agent_usage"]["codex"]["secondary"]["used_pct"] == 89


@pytest.mark.parametrize("window", [
    {"used_pct": -1, "window_minutes": 300},
    {"used_pct": 101, "window_minutes": 300},
    {"used_pct": 20, "window_minutes": 0},
    {"used_pct": 20, "window_minutes": 300, "resets_at": -1},
])
def test_invalid_account_usage_is_rejected(
        client, window):
    """Reject invalid allowance percentages, durations, and reset timestamps at the API."""
    response = client.post("/api/agents/event", json={"session_id": "s", "event": "UsageUpdate", "agent": "claude",
                                                     "usage": {"primary": window}})
    assert response.status_code == 422


def test_normal_hook_updates_usage_without_erasing_other_provider(
        client):
    """A lifecycle event can carry usage; an event without metrics retains the last sample."""
    for kind in ("claude", "codex"):
        client.post("/api/agents/event", json={"session_id": kind, "state": "working", "agent": kind,
                                             "usage": {"primary": {"used_pct": 25, "window_minutes": 300}}})
    client.post("/api/agents/event", json={"session_id": "codex", "event": "PostToolUse", "agent": "codex"})
    assert set(client.app.state.hub.state["agent_usage"]) == {"claude", "codex"}
    assert client.app.state.hub.state["agent_usage"]["codex"]["primary"]["used_pct"] == 25


"""# --- Provider metrics ---"""


def test_codex_context_uses_last_turn_and_latest_account_windows(
        tmp_path):
    """Parse recent Codex token events, separating context from cumulative token totals."""
    transcript = tmp_path / "rollout.jsonl"
    records = [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "model_context_window": 200000, "last_token_usage": {"total_tokens": 50000},
            "total_token_usage": {"total_tokens": 9000000}}, "rate_limits": {
                "limit_id": "codex", "primary": {"used_percent": 12, "window_minutes": 300, "resets_at": 2000000000},
                "secondary": {"used_percent": 0, "window_minutes": 10080}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
            "limit_id": "codex", "primary": {"used_percent": 15, "window_minutes": 300}, "secondary": None}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
            "limit_id": "other-model", "primary": {"used_percent": 99, "window_minutes": 300}}}},
    ]
    transcript.write_text("partial JSON\n" + "\n".join(json.dumps(rec) for rec in records), encoding="utf-8")
    payload = {"session_id": "s", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    event = hook.build_event(payload, {}, "codex")
    assert event["context_pct"] == 25
    assert event["usage"]["primary"]["used_pct"] == 15
    assert event["usage"]["secondary"] is None
    assert "total_tokens" not in json.dumps(event)


def test_codex_notify_finds_only_its_session_in_custom_home(
        tmp_path):
    """Older notify payloads locate their own transcript under CODEX_HOME without a path."""
    sid = "12345678-1234-1234-1234-123456789abc"
    sessions = tmp_path / "sessions" / "2026" / "11" / "03"
    sessions.mkdir(parents=True)
    record = {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "model_context_window": 100000, "last_token_usage": {"total_tokens": 40000}}}}
    (sessions / f"rollout-{sid}.jsonl").write_text(json.dumps(record))
    event = hook.build_notify_event({"type": "agent-turn-complete", "thread-id": sid}, {"CODEX_HOME": str(tmp_path)})
    assert event["context_pct"] == 40
    assert hook.codex_metrics({"session_id": "*"}, {"CODEX_HOME": str(tmp_path)}) == {}
    assert hook.codex_metrics({"transcript_path": str(tmp_path / "missing")}, {}) == {}


def test_claude_statusline_uses_reported_context_and_handles_missing_windows():
    """Keep real zeros, omit absent metrics, and forward no costs, prompts, or credentials."""
    payload = {"session_id": "s", "context_window": {"used_percentage": 0}, "rate_limits": {
        "five_hour": {"used_percentage": 26.5, "resets_at": 2000000000}, "seven_day": None},
        "cost": {"total_cost_usd": 123}, "message": "private"}
    event = hook.build_statusline_event(payload)
    assert event["event"] == "UsageUpdate" and event["context_pct"] == 0
    assert event["usage"]["primary"]["used_pct"] == 26.5
    assert event["usage"]["secondary"] is None
    assert "private" not in json.dumps(event) and "cost" not in event
    assert "usage" not in hook.build_statusline_event({"session_id": "s"})
    assert hook.usage_window({"used_percentage": float("nan")}, "used_percentage", 300) is None


"""# --- Status-line lifecycle ---"""


def test_statusline_install_reinstall_and_uninstall_preserve_original(
        tmp_path):
    """Wrapping must be idempotent and uninstall must restore the exact user configuration."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {"type": "command", "command": "echo original", "padding": 3}
    settings.write_text(json.dumps({"statusLine": original, "theme": "dark"}))
    args = ["--url", "http://localhost:8765", "--no-check", "--claude"]
    assert install.main(args, home=tmp_path) == 0
    installed = settings.read_text()
    assert install.main(args, home=tmp_path) == 0
    assert settings.read_text() == installed
    saved = json.loads((tmp_path / ".config/pideck/hook.json").read_text())
    assert saved["claude_status_line"] == original
    assert install.main(["--uninstall"], home=tmp_path) == 0
    assert json.loads(settings.read_text()) == {"statusLine": original, "theme": "dark"}


def test_statusline_dry_run_and_user_replacement_are_preserved(
        tmp_path):
    """Dry runs write nothing; uninstall retains a user replacement for piDeck's wrapper."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = {"statusLine": {"type": "command", "command": "echo old"}}
    settings.write_text(json.dumps(original))
    args = ["--url", "http://localhost:8765", "--no-check", "--claude"]
    assert install.main([*args, "--dry-run"], home=tmp_path) == 0
    assert json.loads(settings.read_text()) == original
    assert not (tmp_path / ".config").exists()
    assert install.main(args, home=tmp_path) == 0
    data = json.loads(settings.read_text())
    data["statusLine"]["command"] = "echo replacement"
    settings.write_text(json.dumps(data))
    assert install.main(["--uninstall"], home=tmp_path) == 0
    assert json.loads(settings.read_text())["statusLine"]["command"] == "echo replacement"


def test_original_statusline_still_receives_input_when_pideck_is_offline(
        tmp_path, monkeypatch, capfd):
    """A failed piDeck POST must not suppress the original status line or alter its stdin."""
    script = tmp_path / "original.py"
    script.write_text("import sys\nprint('original:' + sys.stdin.read())\n")
    config = tmp_path / "hook.json"
    config.write_text(json.dumps({"claude_status_line": {"command": f'"{sys.executable}" "{script}"'}}))

    def offline(
            event, env):
        """Simulate an unavailable display without waiting for a network timeout."""
        raise OSError("offline")

    monkeypatch.setattr(hook, "post", offline)
    raw = '{"session_id":"s"}'
    hook.run_statusline(raw, {"PIDECK_CONFIG": str(config)})
    assert capfd.readouterr().out.strip() == "original:" + raw
