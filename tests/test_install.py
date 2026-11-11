"""

Hook installation, configuration preservation, and local delivery tests.

"""

from __future__ import annotations

from typing import Any, Iterator

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# --- Tests ---

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import install  # noqa: E402
import pideck_hook  # noqa: E402

URL = "https://pideck.example"


def run(
    home: Path,
    *args: str,
    ) -> int:
    """
    Invoke the installer against a temporary home without a network connectivity check.

    Args:
        home (Path): Home directory containing the provider or integration settings.
        *args (str): Additional arguments accepted by this callback or forwarded to the wrapped
            operation.

    Returns:
        int: Installer exit code for the supplied temporary home and additional options.
    """
    return install.main(["--url", URL, "--no-check", *args], home=home)


def ours(
    settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
    """
    Collect the hook entries belonging to piDeck from provider settings.

    Args:
        settings (dict[str, Any]): Provider settings object whose registered hook entries are
            inspected.

    Returns:
        list[dict[str, Any]]: Flat list of piDeck-owned command entries from every configured event
            group.
    """
    return [h for groups in settings["hooks"].values() for g in groups for h in g["hooks"] if "pideck_hook.py" in h["command"]]


def test_fresh_install_for_claude(
    tmp_path: Path,
    ) -> None:
    """
    Verify fresh install for claude.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    assert run(tmp_path, "--token", "s3cret") == 0
    s = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert sorted(s["hooks"]) == sorted(install.CLAUDE_EVENTS)
    assert all("--source codex" not in h["command"] for h in ours(s))
    conf = json.loads((tmp_path / ".config/pideck/hook.json").read_text())
    assert conf == {"url": URL, "token": "s3cret", "claude_status_line": None}
    assert s["statusLine"]["command"].endswith(" --statusline")
    assert (tmp_path / ".config/pideck/pideck_hook.py").exists()          # copied, repo can move
    if os.name == "posix":
        assert (tmp_path / ".config/pideck/hook.json").stat().st_mode & 0o077 == 0  # token not world-readable
    assert not (tmp_path / ".codex").exists()                              # didn't invent a Codex install


def test_codex_hooks_use_source_flag_and_fewer_events(
    tmp_path: Path,
    ) -> None:
    """
    Verify codex hooks use source flag and fewer events.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".codex").mkdir()
    assert run(tmp_path) == 0
    s = json.loads((tmp_path / ".codex/hooks.json").read_text())
    assert sorted(s["hooks"]) == sorted(install.CODEX_EVENTS)
    assert all(h["command"].endswith("--source codex") for h in ours(s))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows providers use PowerShell hooks")
@pytest.mark.parametrize("source", ["codex", "cursor"])
def test_windows_launcher_preserves_literal_paths_and_stdin(
    tmp_path: Path,
    source: str,
    ) -> None:
    """
    Run the generated hook through real PowerShell with spaces, apostrophes, and dollar signs.

    Args:
        tmp_path (Path): Directory containing an isolated hook receiving public test metadata.
        source (str): Provider whose Windows hook invocation is being exercised.

    Returns:
        None: The executable launches and receives the original arguments and standard input.
    """
    folder = tmp_path / "space's $()"
    folder.mkdir()
    script = folder / "pideck_hook.py"
    script.write_text('"""\n\nEcho isolated launcher-test metadata.\n\n"""\n'
                      'import json,sys\nprint(json.dumps({"args":sys.argv[1:],"input":json.loads(sys.stdin.read())}))\n',
                      encoding="utf-8")
    body = {"session_id": "launch-check", "hook_event_name": "PreToolUse"}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             install.command_for(script, source)],
                            input=json.dumps(body), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"args": ["--source", source], "input": body}


def test_idempotent_and_single_backup(
    tmp_path: Path,
    ) -> None:
    """
    Verify idempotent and single backup.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
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


def test_preserves_users_other_settings_and_hooks(
    tmp_path: Path,
    ) -> None:
    """
    Verify preserves users other settings and hooks.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo lint"}]}
    cf.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [mine], "PreToolUse": [mine]}}))
    run(tmp_path)
    s = json.loads(cf.read_text())
    assert s["model"] == "x" and s["hooks"]["PreToolUse"][0] == mine
    assert mine in s["hooks"]["PostToolUse"] and len(s["hooks"]["PostToolUse"]) == 2


def test_invalid_json_is_left_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ) -> None:
    """
    Verify invalid json is left alone.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        capsys (pytest.CaptureFixture[str]): Pytest fixture capturing text written to standard
            output and error.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    cf.write_text("{ not json")
    assert run(tmp_path) == 1
    assert cf.read_text() == "{ not json"
    assert "isn't valid JSON" in capsys.readouterr().err


def test_uninstall_removes_only_ours(
    tmp_path: Path,
    ) -> None:
    """
    Verify uninstall removes only ours.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    cf = tmp_path / ".claude/settings.json"
    mine = {"hooks": [{"type": "command", "command": "echo keep"}]}
    cf.write_text(json.dumps({"hooks": {"Stop": [mine]}}))
    run(tmp_path)
    assert install.main(["--uninstall"], home=tmp_path) == 0
    assert json.loads(cf.read_text()) == {"hooks": {"Stop": [mine]}}
    assert not (tmp_path / ".config/pideck").exists()


def test_dry_run_changes_nothing(
    tmp_path: Path,
    ) -> None:
    """
    Verify dry run changes nothing.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    assert run(tmp_path, "--dry-run") == 0
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".config").exists()


def test_nothing_installed_gives_a_clear_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    ) -> None:
    """
    Verify nothing installed gives a clear error.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        capsys (pytest.CaptureFixture[str]): Pytest fixture capturing text written to standard
            output and error.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    assert run(tmp_path) == 1
    assert "--claude or --codex" in capsys.readouterr().err


def test_hook_reads_saved_settings_but_env_wins(
    tmp_path: Path,
    ) -> None:
    """
    Verify hook reads saved settings but env wins.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    f = tmp_path / "hook.json"
    f.write_text(json.dumps({"url": "http://pi:1", "token": "filetok"}))
    assert pideck_hook.load_settings({"PIDECK_CONFIG": str(f)}) == {"url": "http://pi:1", "token": "filetok"}
    got = pideck_hook.load_settings({"PIDECK_CONFIG": str(f), "PIDECK_URL": "http://other:2"})
    assert got == {"url": "http://other:2", "token": "filetok"}
    assert pideck_hook.load_settings({"PIDECK_CONFIG": str(tmp_path / "missing.json")})["url"] == "http://localhost:8765"


@pytest.fixture
def live_server(
    tmp_path: Path,
    ) -> Iterator[str]:
    """
    Run a temporary authenticated display for installer integration checks.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        Iterator[str]: Iterator yielding the temporary authenticated display base URL.
    """
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


def test_check_server_ok_and_bad_token(
    live_server: str,
    ) -> None:
    """
    Verify check server ok and bad token.

    Args:
        live_server (str): Base URL of the temporary authenticated display server.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    install.check_server(live_server, "tok123")                            # no exception
    with pytest.raises(install.InstallError, match="401"):
        install.check_server(live_server, "wrong")
    with pytest.raises(install.InstallError, match="Can't reach"):
        install.check_server("http://127.0.0.1:1", "x")


def test_end_to_end_hook_uses_installed_config(
    tmp_path: Path,
    live_server: str,
    ) -> None:
    """
    Install, then run the *installed* hook script with no env vars: the tile must appear.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        live_server (str): Base URL of the temporary authenticated display server.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / ".claude").mkdir()
    assert install.main(["--url", live_server, "--token", "tok123"], home=tmp_path) == 0
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]  # Winsock needs this even in an isolated environment.
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
    subprocess.run([sys.executable, str(tmp_path / ".config/pideck/pideck_hook.py"), "--statusline"],
                   input=json.dumps({"session_id": "e2e", "context_window": {"used_percentage": 33}, "rate_limits": {
                       "five_hour": {"used_percentage": 14, "resets_at": 2000000000}}}),
                   text=True, env=env, check=True, capture_output=True)
    with connect(live_server.replace("http", "ws") + "/ws") as ws:
        snap = json.loads(ws.recv())
    assert snap["state"]["agent_usage"]["claude"]["primary"]["used_pct"] == 14
    agent = next(a for a in snap["state"]["agents"] if a["id"] == "e2e")
    assert agent["context_pct"] == 33 and agent["state"] == "working"
