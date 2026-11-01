import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import install  # noqa: E402
import pideck_hook  # noqa: E402

URL = "http://pideck.local:8765"


def run(home, *args):
    return install.main(["--url", URL, "--no-check", *args], home=home)


def ours(settings):
    return [h for groups in settings["hooks"].values() for g in groups for h in g["hooks"] if "pideck_hook.py" in h["command"]]


def test_fresh_install_for_claude(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert run(tmp_path, "--token", "s3cret") == 0
    s = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert sorted(s["hooks"]) == sorted(install.CLAUDE_EVENTS)
    assert all("--source codex" not in h["command"] for h in ours(s))
    conf = json.loads((tmp_path / ".config/pideck/hook.json").read_text())
    assert conf == {"url": URL, "token": "s3cret"}
    assert (tmp_path / ".config/pideck/pideck_hook.py").exists()          # copied, repo can move
    if os.name == "posix":
        assert (tmp_path / ".config/pideck/hook.json").stat().st_mode & 0o077 == 0  # token not world-readable
    assert not (tmp_path / ".codex").exists()                              # didn't invent a Codex install


def test_codex_hooks_use_source_flag_and_fewer_events(tmp_path):
    (tmp_path / ".codex").mkdir()
    assert run(tmp_path) == 0
    s = json.loads((tmp_path / ".codex/hooks.json").read_text())
    assert sorted(s["hooks"]) == sorted(install.CODEX_EVENTS)
    assert all(h["command"].endswith("--source codex") for h in ours(s))


def test_idempotent_and_single_backup(tmp_path):
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    cf.write_text(json.dumps({"theme": "dark"}))
    run(tmp_path)
    first = cf.read_text()
    run(tmp_path)
    run(tmp_path, "--token", "new")
    assert cf.read_text() == first                                          # no duplicates, no churn
    assert len(ours(json.loads(first))) == len(install.CLAUDE_EVENTS)
    backups = list((tmp_path / ".claude").glob("*.pideck-backup"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == {"theme": "dark"}


def test_preserves_users_other_settings_and_hooks(tmp_path):
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo lint"}]}
    cf.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [mine], "PreToolUse": [mine]}}))
    run(tmp_path)
    s = json.loads(cf.read_text())
    assert s["model"] == "x" and s["hooks"]["PreToolUse"] == [mine]
    assert mine in s["hooks"]["PostToolUse"] and len(s["hooks"]["PostToolUse"]) == 2


def test_invalid_json_is_left_alone(tmp_path, capsys):
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    cf.write_text("{ not json")
    assert run(tmp_path) == 1
    assert cf.read_text() == "{ not json"
    assert "isn't valid JSON" in capsys.readouterr().err


def test_uninstall_removes_only_ours(tmp_path):
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    mine = {"hooks": [{"type": "command", "command": "echo keep"}]}
    cf.write_text(json.dumps({"hooks": {"Stop": [mine]}}))
    run(tmp_path)
    assert install.main(["--uninstall"], home=tmp_path) == 0
    assert json.loads(cf.read_text()) == {"hooks": {"Stop": [mine]}}
    assert not (tmp_path / ".config/pideck").exists()


def test_dry_run_changes_nothing(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert run(tmp_path, "--dry-run") == 0
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".config").exists()


def test_nothing_installed_gives_a_clear_error(tmp_path, capsys):
    assert run(tmp_path) == 1
    assert "--claude or --codex" in capsys.readouterr().err


def test_hook_reads_saved_settings_but_env_wins(tmp_path):
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"url": "http://pi:1", "token": "filetok"}))
    assert pideck_hook.load_settings({"PIDECK_CONFIG": str(f)}) == {"url": "http://pi:1", "token": "filetok"}
    got = pideck_hook.load_settings({"PIDECK_CONFIG": str(f), "PIDECK_URL": "http://other:2"})
    assert got == {"url": "http://other:2", "token": "filetok"}
    assert pideck_hook.load_settings({"PIDECK_CONFIG": str(tmp_path / "missing.json")})["url"] == "http://localhost:8765"


@pytest.fixture
def live_server(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "PIDECK_TOKEN": "tok123"}
    p = subprocess.Popen([sys.executable, "-c",
        f"import uvicorn; from pideck.main import create_app; uvicorn.run(create_app('/nonexistent.yaml', mock=True, lively=False), port={port}, log_level='warning')"],
        cwd=str(ROOT), env=env)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    p.terminate()


def test_check_server_ok_and_bad_token(live_server):
    install.check_server(live_server, "tok123")                            # no exception
    with pytest.raises(install.InstallError, match="401"):
        install.check_server(live_server, "wrong")
    with pytest.raises(install.InstallError, match="Can't reach"):
        install.check_server("http://127.0.0.1:1", "x")


def test_end_to_end_hook_uses_installed_config(tmp_path, live_server):
    """Install, then run the *installed* hook script with no env vars: the tile must appear."""
    (tmp_path / ".claude").mkdir()
    assert install.main(["--url", live_server, "--token", "tok123"], home=tmp_path) == 0
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    subprocess.run([sys.executable, str(tmp_path / ".config/pideck/pideck_hook.py")],
                   input='{"session_id":"e2e","hook_event_name":"UserPromptSubmit","cwd":"/w/demo"}',
                   text=True, env=env, check=True)
    import urllib.request
    # the hook posted with the saved token, so the agent is now on the display
    with urllib.request.urlopen(live_server + "/api/health") as r:
        assert json.loads(r.read())["ok"]
    from websockets.sync.client import connect
    with connect(live_server.replace("http", "ws") + "/ws") as ws:
        snap = json.loads(ws.recv())
    assert [a["name"] for a in snap["state"]["agents"] if a["id"] == "e2e"] == ["demo"]
