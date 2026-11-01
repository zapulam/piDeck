#!/usr/bin/env python3
"""One-command setup for the piDeck hooks (Claude Code and Codex CLI). Stdlib only.

    python3 hooks/install.py --url http://pideck.local:8765 --token YOUR_TOKEN

What it does, and nothing else:
  1. saves the server address/token to ~/.config/pideck/hook.json (readable only by you)
  2. copies the hook script next to it, so this repo can live anywhere (or be deleted)
  3. adds piDeck's hooks to ~/.claude/settings.json and/or ~/.codex/hooks.json, leaving every
     other setting and hook alone, after saving a one-time backup of the file
  4. with --test, makes a test tile appear on the display so you know it works

Run it again any time to update; it never creates duplicates. `--uninstall` removes it all.
"""
import argparse
import json
import os
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MARK = "pideck_hook.py"  # how we recognise our own hook entries

CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Notification", "Stop", "SessionEnd"]
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]  # Codex has no Notification/SessionEnd


class InstallError(Exception):
    pass


def conf_dir(home: Path) -> Path:
    return home / ".config" / "pideck"


def _is_ours(hook: dict) -> bool:
    return MARK in str(hook.get("command", ""))


def strip_ours(hooks: dict) -> dict:
    """Return `hooks` (event -> groups) without any piDeck entries; drop groups that become empty."""
    out = {}
    for event, groups in (hooks or {}).items():
        kept = []
        for g in groups if isinstance(groups, list) else []:
            inner = [h for h in g.get("hooks", []) if not _is_ours(h)]
            if inner:
                kept.append({**g, "hooks": inner})
            elif not g.get("hooks"):
                kept.append(g)  # not a hooks group we understand: leave it be
        if kept:
            out[event] = kept
    return out


def command_for(script: Path, source: str) -> str:
    cmd = f'"{sys.executable}" "{script}"'
    return cmd + (" --source codex" if source == "codex" else "")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except ValueError as e:
        raise InstallError(f"{path} isn't valid JSON ({e}). I haven't touched it; fix or move it and re-run.")
    if not isinstance(data, dict):
        raise InstallError(f"{path} isn't a JSON object. I haven't touched it.")
    return data


def write_json(path: Path, data: dict, dry: bool) -> bool:
    """Write if changed. Returns True if the file changed. Backs up an existing file once."""
    new = json.dumps(data, indent=2) + "\n"
    old = path.read_text() if path.exists() else None
    if old == new:
        return False
    if dry:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        backup = path.with_name(path.name + ".pideck-backup")
        if not backup.exists():  # keep the very first, pre-piDeck copy
            shutil.copy2(path, backup)
    path.write_text(new)
    return True


def install_target(path: Path, events, script: Path, source: str, dry: bool) -> bool:
    data = read_json(path)
    hooks = strip_ours(data.get("hooks", {}))
    for ev in events:
        hooks.setdefault(ev, []).append(
            {"hooks": [{"type": "command", "command": command_for(script, source), "timeout": 5}]}
        )
    data["hooks"] = hooks
    return write_json(path, data, dry)


def uninstall_target(path: Path, dry: bool) -> bool:
    if not path.exists():
        return False
    data = read_json(path)
    if "hooks" not in data:
        return False
    hooks = strip_ours(data["hooks"])
    if hooks:
        data["hooks"] = hooks
    else:
        del data["hooks"]
    return write_json(path, data, dry)


def save_settings(home: Path, url: str, token: str, dry: bool) -> Path:
    d = conf_dir(home)
    f = d / "hook.json"
    if not dry:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"url": url, "token": token}, indent=2) + "\n")
        try:
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # the token lives here: owner only
        except OSError:
            pass
    return f


def copy_script(home: Path, dry: bool) -> Path:
    dest = conf_dir(home) / MARK
    if not dry:
        conf_dir(home).mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(__file__).with_name(MARK), dest)
    return dest


def _request(url, token="", method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers, method=method)
    return urllib.request.urlopen(req, timeout=4).read()


def check_server(url: str, token: str) -> None:
    """Fail early, with a useful message, if the display can't be reached or the token is wrong."""
    try:
        _request(url.rstrip("/") + "/api/health")
    except Exception as e:
        raise InstallError(
            f"Can't reach piDeck at {url} ({e}).\n"
            "  - Is the server running? (python -m pideck)\n"
            "  - Is the address right? Try the Pi's IP instead of pideck.local.\n"
            "  - Use --no-check to install anyway."
        )
    try:  # auth is only enforced on the event endpoint, so probe it with a harmless event
        _request(url.rstrip("/") + "/api/agents/event", token, "POST", {"session_id": "pideck-probe", "event": "SomethingIgnored"})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise InstallError("The server rejected the token (401). Check --token against PIDECK_TOKEN on the Pi.")
        raise InstallError(f"The server answered {e.code} to a test event.")
    except Exception as e:
        raise InstallError(f"Couldn't send a test event ({e}).")


def run_test(url: str, token: str) -> None:
    base = url.rstrip("/")
    sid = "pideck-install-test"
    ev = lambda **kw: _request(base + "/api/agents/event", token, "POST", {"session_id": sid, "name": "install test", **kw})
    ev(state="working")
    print("  A tile named 'install test' should appear on the display...")
    time.sleep(2)
    ev(state="done", message="piDeck hooks are installed")
    print("  ...and a full-screen alert. Cleaning up in a few seconds.")
    time.sleep(6)
    try:
        _request(f"{base}/api/agents/{sid}", method="DELETE")
    except Exception:
        pass


def main(argv=None, home: Path = None) -> int:
    home = home or Path.home()
    ap = argparse.ArgumentParser(description="Install piDeck hooks for Claude Code and/or Codex CLI.")
    ap.add_argument("--url", help="piDeck address, e.g. http://pideck.local:8765")
    ap.add_argument("--token", default="", help="the server's PIDECK_TOKEN, if it has one")
    ap.add_argument("--claude", action="store_true", help="install for Claude Code")
    ap.add_argument("--codex", action="store_true", help="install for Codex CLI")
    ap.add_argument("--test", action="store_true", help="show a test tile on the display afterwards")
    ap.add_argument("--no-check", action="store_true", help="don't verify the server is reachable first")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove piDeck's hooks and saved settings")
    a = ap.parse_args(argv)
    dry = a.dry_run

    claude_file = home / ".claude" / "settings.json"
    codex_file = home / ".codex" / "hooks.json"
    # no flag given: do whatever is installed on this machine
    want_claude = a.claude or (not a.codex and (home / ".claude").exists())
    want_codex = a.codex or (not a.claude and (home / ".codex").exists())

    try:
        if a.uninstall:
            for label, f in (("Claude Code", claude_file), ("Codex", codex_file)):
                if uninstall_target(f, dry):
                    print(f"{'Would remove' if dry else 'Removed'} piDeck hooks from {f} ({label})")
            if not dry:
                shutil.rmtree(conf_dir(home), ignore_errors=True)
            print("Uninstalled." if not dry else "(dry run: nothing changed)")
            return 0

        if not a.url:
            ap.error("--url is required (e.g. --url http://pideck.local:8765)")
        if not (want_claude or want_codex):
            raise InstallError(
                "Couldn't find Claude Code (~/.claude) or Codex (~/.codex) on this machine. "
                "Use --claude or --codex to install for one anyway."
            )
        if not a.no_check:
            check_server(a.url, a.token)
            print(f"Reached piDeck at {a.url}")

        cfg = save_settings(home, a.url, a.token, dry)
        script = copy_script(home, dry)
        print(f"Saved settings to {cfg}")
        if want_claude:
            changed = install_target(claude_file, CLAUDE_EVENTS, script, "claude", dry)
            print(f"Claude Code: {'hooks added to' if changed else 'already up to date in'} {claude_file}")
        if want_codex:
            changed = install_target(codex_file, CODEX_EVENTS, script, "codex", dry)
            print(f"Codex: {'hooks added to' if changed else 'already up to date in'} {codex_file}")
            print("  If Codex doesn't report in, your version may need hooks switched on: add\n"
                  "  [features]\\n  codex_hooks = true   to ~/.codex/config.toml")
        if dry:
            print("(dry run: nothing changed)")
            return 0
        if a.test:
            run_test(a.url, a.token)
        print("Done. Restart any open Claude Code / Codex sessions so they pick up the hooks.")
        return 0
    except InstallError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
