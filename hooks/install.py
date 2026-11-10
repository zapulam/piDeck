"""

One-command setup for piDeck hooks (Claude Code, Codex, and Cursor). Stdlib only.

    python3 hooks/install.py --url https://YOUR_PI_HTTPS_ADDRESS --token YOUR_PIDECK_HOOK_TOKEN

What it does, and nothing else:
  1. saves the server address/token to ~/.config/pideck/hook.json (readable only by you)
  2. copies the hook script next to it, so this repo can live anywhere (or be deleted)
  3. adds piDeck's hooks to ~/.claude/settings.json and/or ~/.codex/hooks.json, leaving every
     other setting and hook alone, after saving a one-time backup of the file
     (Cursor uses flat entries in ~/.cursor/hooks.json; Claude's status line is wrapped)
  4. with --test, makes a test tile appear on the display so you know it works

Run it again any time to update; it never creates duplicates. `--uninstall` removes it all.

"""

from __future__ import annotations

from typing import Any

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .pideck_hook import private_write, safe_open, validate_transport_url
except ImportError:
    from pideck_hook import private_write, safe_open, validate_transport_url

# --- Hook event configuration ---

MARK = "pideck_hook.py"  # how we recognise our own hook entries

CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                 "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop", "SessionEnd"]
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "Interrupt", "SessionEnd"]
CURSOR_EVENTS = ["sessionStart", "beforeSubmitPrompt", "preToolUse", "postToolUse", "postToolUseFailure", "stop", "subagentStart", "subagentStop", "sessionEnd"]


# --- Shared configuration helpers ---


class InstallError(Exception):
    pass


def conf_dir(
    home: Path,
    ) -> Path:
    """
    Locate the piDeck hook settings directory under a user home.

    Args:
        home (Path): Home directory containing the provider or integration settings.

    Returns:
        Path: User-owned directory containing hook settings and the installed standalone script.
    """
    return home / ".config" / "pideck"


def _is_ours(
    hook: dict,
    ) -> bool:
    """
    Identify a hook command that was installed by piDeck.

    Args:
        hook (dict): Lifecycle hook entry to identify as piDeck-owned or unrelated.

    Returns:
        bool: True when the hook command contains the piDeck script marker.
    """
    return MARK in str(hook.get("command", ""))


def strip_ours(
    hooks: dict,
    ) -> dict:
    """
    Return `hooks` (event -> groups) without any piDeck entries; drop groups that become empty.

    Args:
        hooks (dict): Provider hook groups keyed by lifecycle event name.

    Returns:
        dict: Provider hook groups with piDeck-owned entries and newly empty groups removed.
    """
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


def command_for(
    script: Path,
    source: str,
    ) -> str:
    """
    Build a quoted Python command for the installed hook and provider.

    Args:
        script (Path): Standalone piDeck hook script to invoke or copy.
        source (str): Provider identifier, normally claude, codex, or cursor.

    Returns:
        str: Quoted Python command with an explicit provider flag for non-Claude hooks.
    """
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        executable = executable.with_name("piDeck-hook.exe" if sys.platform == "win32" else "piDeck-hook")
    cmd = f'"{executable}" "{script}"'
    return cmd + (f" --source {source}" if source != "claude" else "")


def read_json(
    path: Path,
    ) -> dict:
    """
    Read a settings object or return an empty object when the file is absent.

    Args:
        path (Path): Filesystem path or API path used by this operation.

    Returns:
        dict: Parsed settings dictionary, or an empty dictionary when the file does not exist.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except ValueError as e:
        raise InstallError(f"{path} isn't valid JSON ({e}). I haven't touched it; fix or move it and re-run.")
    if not isinstance(data, dict):
        raise InstallError(f"{path} isn't a JSON object. I haven't touched it.")
    return data


def write_json(
    path: Path,
    data: dict,
    dry: bool,
    ) -> bool:
    """
    Write if changed. Returns True if the file changed. Backs up an existing file once.

    Args:
        path (Path): Filesystem path or API path used by this operation.
        data (dict): JSON-compatible settings or provider fields to inspect or write.
        dry (bool): Whether to report changes without writing any files.

    Returns:
        bool: True when the settings file changed or would change in dry-run mode.
    """
    new = json.dumps(data, indent=2) + "\n"
    old = path.read_text(encoding="utf-8-sig") if path.exists() else None
    if old == new:
        return False
    if dry:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        backup = path.with_name(path.name + ".pideck-backup")
        if not backup.exists():  # keep the very first, pre-piDeck copy
            shutil.copy2(path, backup)
    private_write(path, new)
    return True


def install_target(
    path: Path,
    events: list[str],
    script: Path,
    source: str,
    dry: bool,
    ) -> bool:
    """
    Register piDeck lifecycle hooks without changing unrelated hook entries.

    Args:
        path (Path): Filesystem path or API path used by this operation.
        events (list[str]): Lifecycle event names to register for this provider.
        script (Path): Standalone piDeck hook script to invoke or copy.
        source (str): Provider identifier, normally claude, codex, or cursor.
        dry (bool): Whether to report changes without writing any files.

    Returns:
        bool: True when lifecycle registration changed or would change the settings file.
    """
    data = read_json(path)
    hooks = strip_ours(data.get("hooks", {}))
    for ev in events:
        hooks.setdefault(ev, []).append(
            {"hooks": [{"type": "command", "command": command_for(script, source), "timeout": 5}]}
        )
    data["hooks"] = hooks
    return write_json(path, data, dry)


def uninstall_target(
    path: Path,
    dry: bool,
    ) -> bool:
    """
    Remove piDeck hooks and restore a wrapped status line without touching other settings.

    Args:
        path (Path): Claude or Codex settings JSON file to update.
        dry (bool): Whether to report the change without writing it.

    Returns:
        bool: True when the settings file would change, otherwise False.
    """
    if not path.exists():
        return False
    data = read_json(path)
    has_statusline = _is_ours(data.get("statusLine") or {})
    if "hooks" not in data and not has_statusline:
        return False
    hooks = strip_ours(data.get("hooks", {}))
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if has_statusline:
        saved = read_json(conf_dir(path.parent.parent) / "hook.json")
        original = saved.get("claude_status_line")
        if original is not None:
            data["statusLine"] = original
        else:
            data.pop("statusLine", None)
    return write_json(path, data, dry)


# --- Claude usage integration ---


def install_statusline(
    home: Path,
    path: Path,
    script: Path,
    dry: bool,
    ) -> bool:
    """
    Wrap Claude's status line to forward usage while preserving its current output.

    Args:
        home (Path): Home directory holding the per-user piDeck settings.
        path (Path): Claude settings JSON path.
        script (Path): Installed piDeck hook script path.
        dry (bool): Whether to report changes without writing files.

    Returns:
        bool: True when the Claude settings would change. Reinstallation is idempotent.
    """
    data = read_json(path)
    original = data.get("statusLine")
    if _is_ours(original or {}):
        data["statusLine"] = {**original, "command": command_for(script, "claude") + " --statusline",
                              "refreshInterval": 60}
        return write_json(path, data, dry)
    if not dry:
        cfg = conf_dir(home) / "hook.json"
        saved = read_json(cfg)
        saved["claude_status_line"] = original
        private_write(cfg, json.dumps(saved, indent=2) + "\n")
    data["statusLine"] = {
        **(original or {}),
        "type": "command",
        "command": command_for(script, "claude") + " --statusline",
        "refreshInterval": 60,
    }
    return write_json(path, data, dry)


# --- Cursor hook integration ---


def update_cursor_hooks(
    path: Path,
    script: Path,
    dry: bool,
    uninstall: bool = False,
    ) -> bool:
    """
    Install or remove piDeck's flat Cursor hooks while retaining unrelated entries.

    Args:
        path (Path): User-level .cursor/hooks.json file.
        script (Path): Installed hook script path; unused during removal.
        dry (bool): Whether to report changes without writing settings.
        uninstall (bool): Whether to remove rather than install piDeck entries.

    Returns:
        bool: True when the settings file would change, otherwise False.

    Raises:
        InstallError: The existing version or hook structure cannot be safely edited.
    """
    if uninstall and not path.exists():
        return False
    data = read_json(path)
    if data.get("version", 1) != 1:
        raise InstallError(f"{path} has an unsupported Cursor hooks version; it was left unchanged.")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(
            not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries)
            for entries in hooks.values()):
        raise InstallError(f"{path} has an unsupported Cursor hooks structure; it was left unchanged.")
    kept = {}
    removed = False
    for event, entries in hooks.items():
        remaining = [entry for entry in entries if not _is_ours(entry)]
        removed = removed or len(remaining) != len(entries)
        if remaining or not entries:
            kept[event] = remaining
    if uninstall and not removed:
        return False
    if not uninstall:
        for event in CURSOR_EVENTS:
            kept.setdefault(event, []).append({"command": command_for(script, "cursor"), "timeout": 5})
        data.setdefault("version", 1)
    data["hooks"] = kept
    return write_json(path, data, dry)


# --- Shared settings and installation ---


def save_settings(
    home: Path,
    url: str,
    token: str,
    dry: bool,
    ) -> Path:
    """
    Persist the display URL and token while preserving existing hook preferences.

    Args:
        home (Path): Home directory containing the provider or integration settings.
        url (str): HTTP or HTTPS address of the piDeck display.
        token (str): Access token used only for the intended local or provider request.
        dry (bool): Whether to report changes without writing any files.

    Returns:
        Path: Path to the saved or proposed hook settings file.
    """
    d = conf_dir(home)
    f = d / "hook.json"
    if not dry:
        d.mkdir(parents=True, exist_ok=True)
        try:
            conf = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            conf = {}
        conf["url"] = url
        if token or "token" not in conf:
            conf["token"] = token
        private_write(f, json.dumps(conf, indent=2) + "\n")
    return f


def copy_script(
    home: Path,
    dry: bool,
    ) -> Path:
    """
    Copy the standalone lifecycle hook into the user settings directory.

    Args:
        home (Path): Home directory containing the provider or integration settings.
        dry (bool): Whether to report changes without writing any files.

    Returns:
        Path: Path to the installed or proposed standalone hook script.
    """
    dest = conf_dir(home) / MARK
    if not dry:
        conf_dir(home).mkdir(parents=True, exist_ok=True)
        for name in (MARK, "event_store.py", "runtime.py"):
            private_write(conf_dir(home) / name, Path(__file__).with_name(name).read_text(encoding="utf-8"))
    return dest


def install_engine_bridge(
    home: Path,
    script: Path,
    dry: bool,
    ) -> None:
    """
    Forward Claude Desktop engine measurements without opening account credential files.

    Args:
        home (Path): User home containing the provider's mods directory.
        script (Path): Installed metadata-only hook helper.
        dry (bool): Whether to report a proposed installation without writing files.

    Returns:
        None: An isolated piDeck mod is installed; unrelated provider mods stay untouched.
    """
    if dry:
        return
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        executable = executable.with_name("piDeck-hook.exe" if sys.platform == "win32" else "piDeck-hook")
    argv = [str(executable), str(script), "--engine"]
    template = Path(__file__).with_name("claude_engine.ts").read_text(encoding="utf-8")
    directory = home / ".claude/mods/pideck-usage/hooks"
    private_write(directory / "register.ts", template.replace("__COMMAND__", json.dumps(argv)))
    private_write(directory / "hooks.json", '{"modules":["./register.ts"]}\n')
    private_write(directory.parent / ".claude-plugin/plugin.json", json.dumps({
        "name": "pideck-usage", "version": "1.0.0",
        "description": "Report numeric engine usage to the local piDeck helper.",
    }) + "\n")
    settings_path = home / ".claude/settings.json"
    settings = read_json(settings_path)
    environment = settings.setdefault("env", {})
    paths = [path for path in environment.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep) if path]
    if str(directory.parent) not in paths:
        paths.append(str(directory.parent))
    environment["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(paths)
    write_json(settings_path, settings, False)


def _request(
    url: str,
    token: str = '',
    method: str = 'GET',
    body: dict[str, Any] | None = None,
    ) -> bytes:
    """
    Send an authenticated request to the display and return its response bytes.

    Args:
        url (str): HTTP or HTTPS address of the piDeck display.
        token (str): Access token used only for the intended local or provider request.
        method (str): HTTP method to use for the request.
        body (dict[str, Any] | None): Validated request body or optional JSON fields to send.

    Returns:
        bytes: Raw response bytes from the display endpoint.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, headers, method=method)
    with safe_open(req, timeout=4) as response:
        return response.read(65536)


def check_server(
    url: str,
    token: str,
    ) -> None:
    """
    Fail early, with a useful message, if the display can't be reached or the token is wrong.

    Args:
        url (str): HTTP or HTTPS address of the piDeck display.
        token (str): Access token used only for the intended local or provider request.

    Returns:
        None: No value is returned.
    """
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


def run_test(
    url: str,
    token: str,
    ) -> None:
    """
    Post a temporary agent lifecycle event to check display connectivity.

    Args:
        url (str): HTTP or HTTPS address of the piDeck display.
        token (str): Access token used only for the intended local or provider request.

    Returns:
        None: No value is returned.
    """
    base = url.rstrip("/")
    sid = "pideck-install-test"
    ev = lambda **kw: _request(base + "/api/agents/event", token, "POST", {"session_id": sid, "name": "install test", **kw})
    ev(state="working")
    print("  A tile named 'install test' should appear on the display...")
    time.sleep(2)
    ev(state="done", message="piDeck hooks are installed")
    print("  ...and a full-screen alert. Use Dismiss on the test card when finished.")


def main(
    argv: list[str] | None = None,
    home: Path | None = None,
    ) -> int:
    """
    Install or remove supported provider integrations using the requested options.

    Args:
        argv (list[str] | None): Command-line arguments, or None to read the current process
            arguments.
        home (Path | None): Home directory containing the provider or integration settings.

    Returns:
        int: Zero on success or a deliberate no-op, or one when installation validation fails.
    """
    home = home or Path.home()
    ap = argparse.ArgumentParser(description="Install piDeck hooks for Claude Code, Codex, and Cursor.")
    ap.add_argument("--url", help="piDeck address, e.g. http://pideck.local:8765")
    ap.add_argument("--token", default=os.environ.get("PIDECK_HOOK_TOKEN", ""), help="the server's event-only PIDECK_HOOK_TOKEN")
    ap.add_argument("--local-config", type=Path, help="initialize event credentials for an owned loopback server")
    ap.add_argument("--claude", action="store_true", help="install for Claude Code")
    ap.add_argument("--codex", action="store_true", help="install for Codex CLI")
    ap.add_argument("--cursor", action="store_true", help="install for Cursor")
    ap.add_argument("--test", action="store_true", help="show a test tile on the display afterwards")
    ap.add_argument("--no-check", action="store_true", help="don't verify the server is reachable first")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove piDeck's hooks and saved settings")
    a = ap.parse_args(argv)
    dry = a.dry_run

    claude_file = home / ".claude" / "settings.json"
    codex_file = home / ".codex" / "hooks.json"
    cursor_file = home / ".cursor" / "hooks.json"
    # no flag given: do whatever is installed on this machine
    auto = not (a.claude or a.codex or a.cursor)
    want_claude = a.claude or (auto and (home / ".claude").exists())
    want_codex = a.codex or (auto and (home / ".codex").exists())
    want_cursor = a.cursor or (auto and (home / ".cursor").exists())

    try:
        if a.uninstall:
            for label, f in (("Claude Code", claude_file), ("Codex", codex_file)):
                if uninstall_target(f, dry):
                    print(f"{'Would remove' if dry else 'Removed'} piDeck hooks from {f} ({label})")
            if update_cursor_hooks(cursor_file, conf_dir(home) / MARK, dry, uninstall=True):
                print(f"{'Would remove' if dry else 'Removed'} piDeck hooks from {cursor_file} (Cursor)")
            if not dry:
                settings = read_json(claude_file)
                environment = settings.get("env", {})
                bridge = home / ".claude/mods/pideck-usage"
                paths = [path for path in environment.get("CLAUDE_CODE_PLUGIN_DIRS", "").split(os.pathsep)
                         if path and path != str(bridge)]
                if paths:
                    environment["CLAUDE_CODE_PLUGIN_DIRS"] = os.pathsep.join(paths)
                else:
                    environment.pop("CLAUDE_CODE_PLUGIN_DIRS", None)
                if not environment:
                    settings.pop("env", None)
                if claude_file.is_file():
                    write_json(claude_file, settings, False)
                for owned in (bridge / "hooks/register.ts", bridge / "hooks/hooks.json", bridge / ".claude-plugin/plugin.json"):
                    owned.unlink(missing_ok=True)
                shutil.rmtree(conf_dir(home), ignore_errors=True)
            print("Uninstalled." if not dry else "(dry run: nothing changed)")
            return 0

        if not a.url:
            ap.error("--url is required (e.g. --url http://pideck.local:8765)")
        validate_transport_url(a.url)
        if a.local_config is not None:
            from urllib.parse import urlsplit
            from pideck.config import load_config
            from pideck.__main__ import ensure_token

            if urlsplit(a.url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise InstallError("Local configuration credentials may only be sent to loopback")
            load_config(a.local_config)
            ensure_token(str(a.local_config))
            a.token, _ = ensure_token(str(a.local_config), "PIDECK_HOOK_TOKEN")
        if not (want_claude or want_codex or want_cursor):
            raise InstallError(
                "Couldn't find Claude Code (~/.claude), Codex (~/.codex), or Cursor (~/.cursor) on this machine. "
                "Use --claude or --codex or --cursor to install for one anyway."
            )
        if not a.no_check:
            check_server(a.url, a.token)
            print(f"Reached piDeck at {a.url}")

        cfg = save_settings(home, a.url, a.token, dry)
        script = copy_script(home, dry)
        print(f"Saved settings to {cfg}")
        if want_claude:
            changed = install_target(claude_file, CLAUDE_EVENTS, script, "claude", dry)
            changed = install_statusline(home, claude_file, script, dry) or changed
            install_engine_bridge(home, script, dry)
            print(f"Claude Code: {'hooks added to' if changed else 'already up to date in'} {claude_file}")
            print("  Claude Desktop usage connects through its engine mod (v2.1.280+); CLI status-line output is preserved.")
        if want_codex:
            changed = install_target(codex_file, CODEX_EVENTS, script, "codex", dry)
            print(f"Codex: {'hooks added to' if changed else 'already up to date in'} {codex_file}")
            print("  Review the updated hooks in Codex if prompted. Account usage uses its local app-server.")
        if want_cursor:
            changed = update_cursor_hooks(cursor_file, script, dry)
            print(f"Cursor: {'hooks added to' if changed else 'already up to date in'} {cursor_file}")
            print("  Context comes from local conversation metadata; personal account allowances are unavailable.")
        if dry:
            print("(dry run: nothing changed)")
            return 0
        if a.test:
            run_test(a.url, a.token)
        print("Done. Restart any open Claude Code / Codex / Cursor sessions so they pick up the hooks.")
        return 0
    except (InstallError, ValueError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
