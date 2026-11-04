#!/usr/bin/env python3
"""Claude Code / Codex / Cursor hook -> piDeck. Stdlib only and never allowed to break your session.

Two ways in, same script:

  Hooks (Claude Code and Codex CLI):  the tool pipes a JSON payload to stdin
      (session_id, cwd, hook_event_name, ...). Tell us which tool it is with --source:
          python3 pideck_hook.py                    # Claude Code (default)
          python3 pideck_hook.py --source codex     # Codex CLI
          python3 pideck_hook.py --source cursor    # Cursor's camelCase hook events

  Codex `notify` (fallback if you don't use hooks): Codex passes ONE JSON string as the last
  command-line argument instead of stdin and only reports "turn finished". Detected automatically.

  Claude status line (--statusline): forwards context and account usage, then runs the saved
  original status-line command. The installer configures this wrapper automatically.

Easiest setup: run hooks/install.py, which saves the address in ~/.config/pideck/hook.json
and registers this script. Environment variables, if set, override that file:
  PIDECK_URL             default http://localhost:8765   (use the Pi's address, e.g. http://pideck.local:8765)
  PIDECK_TOKEN           shared secret, if the server has one
  PIDECK_CONFIG          path to a different hook.json
  PIDECK_NAME            tile label override (default: the project folder name)
  PIDECK_CONTEXT_WINDOW  tokens for the ctx% estimate (default 200000; Claude Code only)
  CODEX_HOME             Codex data directory (default ~/.codex), used to find session metrics
  PIDECK_SEND_MESSAGES   set to 1 to also send a 200-character snippet of the assistant's last reply
                         (off by default: it crosses your network, and the server may be plain HTTP).
                         Or set "send_messages": true in ~/.config/pideck/hook.json.
"""
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TAIL_BYTES = 256 * 1024
CURSOR_EVENTS = {
    "sessionStart": "SessionStart",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUse",
    "stop": "Stop",
    "sessionEnd": "SessionEnd",
    "preCompact": "UsageUpdate",
    "subagentStart": "UserPromptSubmit",
    "subagentStop": "Stop",
}
PRIME_LIMIT = 24
WORKING_AGE_SECONDS = 120


"""# --- Usage helpers ---"""


def transcript_records(
        transcript_path,
):
    """Yield recent JSON objects newest first, tolerating missing or truncated logs.

    Args:
        transcript_path: Local session transcript to read, capped at TAIL_BYTES.

    Yields:
        Parsed dictionary records; malformed lines and non-object records are skipped.
    """
    try:
        with open(transcript_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - TAIL_BYTES))
            lines = stream.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec
    except (OSError, TypeError):
        return


def _reset_timestamp(
        value,
):
    """Convert a provider reset time to Unix seconds.

    Args:
        value: Unix seconds, milliseconds, or an ISO-8601 timestamp.

    Returns:
        A non-negative Unix timestamp, or None when the value is absent or unusable.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            try:
                reset = float(text)
            except ValueError:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                reset = parsed.timestamp()
        else:
            reset = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(reset) or reset < 0:
        return None
    if reset > 10_000_000_000:  # providers sometimes send milliseconds
        reset /= 1000
    return reset


def usage_window(
        data,
        percent_key,
        minutes,
):
    """Normalize one provider window, returning None for absent or invalid metrics.

    Args:
        data: Provider window dictionary, or None when unavailable.
        percent_key: Provider-specific key containing percent used.
        minutes: Duration of the allowance window in minutes.

    Returns:
        Only the numeric fields accepted by piDeck, without any account credentials.
    """
    if not isinstance(data, dict):
        return None
    try:
        pct = None
        for key in (percent_key, "used_percentage", "used_percent", "utilization"):
            if key in data and data[key] is not None:
                pct = float(data[key])
                break
        if pct is None:
            return None
        duration = int(minutes)
        if not math.isfinite(pct) or not 0 <= pct <= 100 or not 0 < duration <= 525600:
            return None
        reset = _reset_timestamp(data.get("resets_at", data.get("resetsAt")))
        return {"used_pct": pct, "window_minutes": duration, "resets_at": reset}
    except (TypeError, ValueError, OverflowError):
        return None


def codex_metrics(
        payload,
        env,
):
    """Read context and account usage from this Codex session's latest token events.

    Args:
        payload: Hook or notify payload, optionally containing transcript_path.
        env: Environment mapping; CODEX_HOME overrides the default ~/.codex directory.

    Returns:
        Available context_pct and usage fields. Missing data is never treated as zero.
    """
    path = payload.get("transcript_path")
    if not path:
        sid = payload.get("session_id") or payload.get("thread-id") or ""
        # Only search for a real session ID, never interpolate arbitrary hook input into a glob.
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", sid):
            return {}
        root = Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
        try:
            paths = list(root.glob(f"**/*{sid}*.jsonl"))
            path = max(paths, key=lambda candidate: candidate.stat().st_mtime) if paths else None
        except OSError:
            return {}
        if not path:
            return {}
    result = {}
    for rec in transcript_records(path):
        data = rec.get("payload") or {}
        if rec.get("type") != "event_msg" or not isinstance(data, dict) or data.get("type") != "token_count":
            continue
        info = data.get("info") or {}
        if "context_pct" not in result and isinstance(info, dict):
            last = info.get("last_token_usage") or {}
            window = info.get("model_context_window")
            used = last.get("total_tokens") if isinstance(last, dict) else None
            if (isinstance(used, (int, float)) and isinstance(window, (int, float))
                    and math.isfinite(used) and math.isfinite(window) and window > 0 and used >= 0):
                result["context_pct"] = round(min(100.0, used * 100.0 / window), 1)
        limits = data.get("rate_limits") or {}
        if ("usage" not in result and isinstance(limits, dict) and limits
                and limits.get("limit_id") in (None, "codex")):
            result["usage"] = {
                key: usage_window(limits.get(key), "used_percent", limits[key].get("window_minutes"))
                if isinstance(limits.get(key), dict) else None
                for key in ("primary", "secondary")
            }
        if "context_pct" in result and "usage" in result:
            break
    return result


def context_pct(transcript_path, window):
    """Best-effort (Claude Code transcripts): latest assistant prompt tokens / window. None if unknown."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # first line of the tail may be cut off
            usage = (rec.get("message") or {}).get("usage")
            if rec.get("type") == "assistant" and usage:
                used = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
                return round(min(100.0, used * 100.0 / window), 1)
    except Exception:
        pass
    return None


def _saved(env):
    path = env.get("PIDECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "pideck", "hook.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def wants_messages(env):
    v = env.get("PIDECK_SEND_MESSAGES")
    return v.lower() in ("1", "true", "yes") if v is not None else bool(_saved(env).get("send_messages"))


"""# --- Event builders ---"""


def build_event(
        payload,
        env,
        source="claude",
):
    """Build a lifecycle event with optional context and account-usage metrics.

    Args:
        payload: Claude Code or Codex lifecycle hook input.
        env: Hook environment controlling display names, messages, and local paths.
        source: Provider identifier, normally claude or codex.

    Returns:
        A piDeck event containing only the selected display fields and metrics.
    """
    if source == "cursor":
        return build_cursor_event(payload, env)
    ev = {
        "session_id": payload.get("session_id") or "unknown",
        "event": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
        "agent": source,
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    hook_name = payload.get("hook_event_name")
    if hook_name in ("SubagentStart", "SubagentStop") and payload.get("agent_id"):
        parent = payload.get("session_id") or "unknown"
        ev["parent_id"] = str(parent)[:128]
        ev["session_id"] = (str(parent) + ":" + _token(payload["agent_id"]))[:128]
        ev["event"] = "Stop" if hook_name == "SubagentStop" else "UserPromptSubmit"
        if payload.get("agent_type"):
            ev["name"] = str(payload["agent_type"]).strip()[:64]
    # Notification carries `message`; Codex's Stop carries `last_assistant_message`
    msg = payload.get("message") or (payload.get("last_assistant_message") if wants_messages(env) else None)
    if msg:
        ev["message"] = " ".join(str(msg).split())[:200]
    if source == "codex" and ev["event"] != "SessionEnd":
        ev.update(codex_metrics(payload, env))
    if source == "claude" and payload.get("transcript_path") and ev["event"] != "SessionEnd":
        pct = context_pct(payload["transcript_path"], float(env.get("PIDECK_CONTEXT_WINDOW", 200000)))
        if pct is not None:
            ev["context_pct"] = pct
    return ev


def _token(
        value,
        limit=60,
):
    """Keep an identifier short and free of whitespace.

    Args:
        value: Raw id from a hook payload or a file name.
        limit: Maximum characters to keep.

    Returns:
        A single-line identifier, or an empty string.
    """
    return "".join(str(value or "").split())[:limit]


def cursor_subagent_ids(
        payload,
):
    """Session and parent ids for a Cursor subagent, without its task text.

    Args:
        payload: subagentStart or subagentStop input.

    Returns:
        (session_id, parent_id). Either is None when the payload has no identity.
    """
    parent = payload.get("parent_conversation_id") or payload.get("conversation_id")
    sub = payload.get("subagent_id") or payload.get("tool_call_id")
    if not sub and payload.get("agent_transcript_path"):
        sub = Path(str(payload["agent_transcript_path"])).stem
    if not sub and parent and payload.get("subagent_type"):
        sub = _token(parent, 40) + ":" + _token(payload.get("subagent_type"), 24)
    if not sub:
        return None, None
    parent_id = ("cursor:" + _token(parent, 80))[:128] if parent else None
    return ("cursor:sub:" + _token(sub, 80))[:128], parent_id


def cursor_hook_response(
        event,
):
    """The JSON Cursor requires before it will continue a permission hook.

    Args:
        event: Cursor hook_event_name.

    Returns:
        A schema-valid response. subagentStart must allow, or Cursor blocks the child.
    """
    if event == "beforeSubmitPrompt":
        return {"continue": True}
    if event in ("preToolUse", "subagentStart"):
        return {"permission": "allow"}
    return {}


def cursor_detail(
        payload,
):
    """A short public label for one Cursor chat, never a prompt or account identity.

    Args:
        payload: Cursor hook input. Only the composer mode and model slug are read.

    Returns:
        Ask or edit mode plus the model slug, trimmed to 64 characters, or None.
    """
    parts = []
    mode = payload.get("composer_mode")
    if isinstance(mode, str) and mode.strip().lower() in {"ask", "edit"}:
        parts.append(mode.strip().lower())
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        parts.append(" ".join(model.split())[:48])
    label = " · ".join(parts)[:64]
    return label or None


def build_cursor_event(
        payload,
        env,
):
    """Translate Cursor hooks without forwarding prompts, tool inputs, or account identity.

    Args:
        payload: Cursor's documented hook input, keyed by conversation_id.
        env: Environment mapping supporting the PIDECK_NAME tile label override.

    Returns:
        A piDeck lifecycle or context-only event, or None for unrelated hooks.
        Context is cleared on later activity because preCompact is only a snapshot.
    """
    hook_name = payload.get("hook_event_name")
    event = CURSOR_EVENTS.get(hook_name)
    sub_session, parent_id = cursor_subagent_ids(payload) if hook_name in ("subagentStart", "subagentStop") else (None, None)
    sid = payload.get("conversation_id") or payload.get("session_id") or (sub_session or "").removeprefix("cursor:")
    if event is None or not (sid or sub_session):
        return None
    roots = payload.get("workspace_roots") or []
    ev = {
        "session_id": sub_session or ("cursor:" + str(sid))[:128],
        "event": event,
        "agent": "cursor",
        "cwd": payload.get("cwd") or (roots[0] if roots else None),
        "context_pct": None,
    }
    if parent_id:
        ev["parent_id"] = parent_id
    if hook_name in ("subagentStart", "subagentStop") and payload.get("subagent_type"):
        ev["name"] = str(payload["subagent_type"]).strip()[:64]
    elif env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    model = payload.get("subagent_model")
    if isinstance(model, str) and model.strip():
        ev["detail"] = " ".join(model.split())[:64]
    else:
        detail = cursor_detail(payload)
        if detail:
            ev["detail"] = detail
    if event == "UsageUpdate":
        pct = payload.get("context_usage_percent")
        if isinstance(pct, (int, float)) and math.isfinite(pct) and 0 <= pct <= 100:
            ev["context_pct"] = pct
    elif event == "Stop":
        status = payload.get("status")
        if status == "error":
            ev["state"] = "error"
        elif status == "aborted":
            ev["state"] = "idle"
    elif payload.get("hook_event_name") == "postToolUseFailure" and payload.get("failure_type") == "permission_denied":
        ev["state"] = "waiting"
    return ev


def build_notify_event(payload, env):
    """Codex `notify` payload (kebab-case keys). Only 'agent-turn-complete' exists today -> done."""
    if payload.get("type") != "agent-turn-complete":
        return None
    ev = {
        "session_id": payload.get("thread-id") or payload.get("turn-id") or "codex",
        "event": "Stop",
        "cwd": payload.get("cwd"),
        "agent": "codex",
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    if payload.get("last-assistant-message") and wants_messages(env):
        ev["message"] = " ".join(str(payload["last-assistant-message"]).split())[:200]
    ev.update(codex_metrics(payload, env))
    return ev


def build_statusline_event(
        payload,
):
    """Convert Claude's documented status-line metrics without changing session state.

    Args:
        payload: Claude Code status-line JSON received on stdin.

    Returns:
        A usage-only event containing only context, account percentages, and identity.
    """
    limits = payload.get("rate_limits")
    ev = {"session_id": payload.get("session_id") or "unknown", "event": "UsageUpdate", "agent": "claude"}
    pct = (payload.get("context_window") or {}).get("used_percentage")
    if isinstance(pct, (int, float)) and math.isfinite(pct) and 0 <= pct <= 100:
        ev["context_pct"] = pct
    if isinstance(limits, dict):
        ev["usage"] = {
            "primary": usage_window(limits.get("five_hour"), "used_percentage", 300),
            "secondary": usage_window(limits.get("seven_day"), "used_percentage", 10080),
        }
    return ev


def run_statusline(
        raw,
        env,
):
    """Forward Claude metrics and retain the user's existing status-line output.

    Args:
        raw: Unmodified JSON input, also passed to the original status-line command.
        env: Environment mapping used to load piDeck's settings.
    """
    payload = {}
    try:
        payload = json.loads(raw)
        post(build_statusline_event(payload), env)
    except Exception:
        pass
    original = (_saved(env).get("claude_status_line") or {}).get("command")
    if original:
        # This is the user's own configured shell command, never data from the payload.
        subprocess.run(original, input=raw, text=True, shell=True, timeout=3, check=False)
    else:
        pct = (payload.get("context_window") or {}).get("used_percentage")
        print(f"Claude · {pct:g}% ctx" if isinstance(pct, (int, float)) else "Claude")


"""# --- Hook transport and entry point ---"""


def parse_args(argv):
    """-> (source, notify_json_or_None)"""
    source = "claude"
    rest = list(argv)
    if "--source" in rest:
        i = rest.index("--source")
        if i + 1 < len(rest):
            source = rest[i + 1]
            del rest[i:i + 2]
    if rest and rest[-1].lstrip().startswith("{"):
        return "codex", rest[-1]
    return source, None


def load_settings(env):
    """url/token: environment wins, then the file the installer wrote, then defaults.
    The file matters because apps started from a dock or Start menu don't see shell exports."""
    saved = _saved(env)
    return {
        "url": env.get("PIDECK_URL") or saved.get("url") or "http://localhost:8765",
        "token": env.get("PIDECK_TOKEN") or saved.get("token") or "",
    }


def usage_cache_path(
        env,
):
    """Where the last account-usage sample is kept so a later launch can show it.

    Args:
        env: Environment mapping. PIDECK_CONFIG selects a different hook directory.

    Returns:
        The cache path beside hook.json. The file holds percentages only.
    """
    saved = env.get("PIDECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "pideck", "hook.json")
    return Path(saved).parent / "usage-cache.json"


def remember_usage(
        ev,
        env,
):
    """Store one provider's allowance sample for the next piDeck launch.

    Args:
        ev: Event that may contain an agent name and a usage object.
        env: Environment mapping used to locate the cache.
    """
    usage = ev.get("usage") if isinstance(ev, dict) else None
    agent = ev.get("agent") if isinstance(ev, dict) else None
    if agent not in ("claude", "codex", "cursor") or not isinstance(usage, dict):
        return
    path = usage_cache_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if path.is_file():
            current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
        current[agent] = {"usage": usage, "updated_at": time.time()}
        path.write_text(json.dumps(current), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except (OSError, ValueError):
        return


def cached_usage_events(
        env,
):
    """Turn the saved allowance sample into usage-only events.

    Args:
        env: Environment mapping used to locate the cache.

    Returns:
        One UsageUpdate per cached provider. An unreadable cache yields nothing.
    """
    try:
        data = json.loads(usage_cache_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    events = []
    for agent in ("claude", "codex", "cursor"):
        item = data.get(agent)
        if isinstance(item, dict) and isinstance(item.get("usage"), dict):
            events.append({
                "session_id": "usage:" + agent,
                "event": "UsageUpdate",
                "agent": agent,
                "usage": item["usage"],
            })
    return events


def _window(
        percent,
        minutes,
        resets_at,
        label,
):
    """Build one allowance window, or None when the percent is unusable.

    Args:
        percent: Percent used, already numeric.
        minutes: Window length in minutes.
        resets_at: Unix reset time, or None.
        label: Short bar caption such as Included or API.

    Returns:
        A piDeck usage window, or None.
    """
    window = usage_window({"used_percentage": percent, "resets_at": resets_at}, "used_percentage", minutes)
    if window and label:
        window["label"] = label
    return window


def cursor_usage_from_body(
        payload,
):
    """Read Cursor's current-period usage payload down to two public bars.

    Args:
        payload: JSON object from DashboardService/GetCurrentPeriodUsage.

    Returns:
        A usage object with Included and API windows, or None when no percent is present.
    """
    if not isinstance(payload, dict):
        return None
    plan = payload.get("planUsage") if isinstance(payload.get("planUsage"), dict) else {}
    reset = payload.get("billingCycleEnd")
    # The dashboard sentence "N% of your included usage" is included spend over the plan limit.
    # totalPercentUsed is a different internal mix and is only a fallback.
    limit = plan.get("limit")
    spent = plan.get("includedSpend")
    if isinstance(limit, (int, float)) and isinstance(spent, (int, float)) and limit > 0 and spent >= 0:
        included = min(100.0, spent * 100.0 / limit)
    else:
        included = plan.get("totalPercentUsed")
    api = plan.get("apiPercentUsed")
    auto = plan.get("autoPercentUsed")
    primary = _window(included if included is not None else auto, 43200, reset, "Included")
    secondary_pct = api if api is not None else (auto if included is not None else None)
    secondary = _window(secondary_pct, 43200, reset, "API" if api is not None else "Auto")
    if primary is None and secondary is None:
        return None
    return {"primary": primary, "secondary": secondary}


def cursor_state_db(
        env,
):
    """Locate Cursor's local state database without opening it.

    Args:
        env: Environment mapping. CURSOR_STATE_DB overrides the platform path.

    Returns:
        Path to state.vscdb.
    """
    override = env.get("CURSOR_STATE_DB")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def read_cursor_token(
        env,
):
    """Read the local Cursor access token. Callers must not log or forward it.

    Args:
        env: Environment mapping used to find state.vscdb.

    Returns:
        The access token, or None when the database or the key is unavailable.
    """
    path = cursor_state_db(env)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute(
                "select value from ItemTable where key = ? limit 1",
                ("cursorAuth/accessToken",),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or row[0] in (None, ""):
        return None
    value = row[0].decode("utf-8", "ignore") if isinstance(row[0], bytes) else str(row[0]).strip()
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, str) and value else None


def fetch_cursor_usage(
        env,
        opener=None,
):
    """Ask Cursor's usage service for percentages. The token is not included in the result.

    Args:
        env: Environment mapping used to find the local sign-in.
        opener: Callable matching urllib.request.urlopen, for tests.

    Returns:
        A usage-only Cursor event, or None when the sign-in or the service is unavailable.
    """
    token = read_cursor_token(env)
    if not token:
        return None
    request = urllib.request.Request(
        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        data=b"{}",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except (OSError, ValueError):
        return None
    usage = cursor_usage_from_body(body)
    if not usage:
        return None
    return {"session_id": "usage:cursor", "event": "UsageUpdate", "agent": "cursor", "usage": usage}


def _header_fields(
        path,
        wanted,
):
    """Read a few named fields from the start of a session file, never the message body.

    Args:
        path: JSONL session file.
        wanted: Field names to copy when present on an object.

    Returns:
        The first values found for those names.
    """
    found = {}
    try:
        with open(path, "rb") as stream:
            raw = stream.read(8192).decode("utf-8", "ignore")
    except OSError:
        return found
    # Pull named strings out of the prefix. Parsing the whole line would also read
    # Codex's base instructions, which are not a display field.
    for key in wanted:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', raw)
        if not match:
            continue
        try:
            value = json.loads('"' + match.group(1) + '"')
        except ValueError:
            continue
        if isinstance(value, str) and value.strip():
            found[key] = value.strip()
    return found


def _recent_files(
        root,
        pattern,
        now,
        stale_seconds,
):
    """Newest files under root whose mtime is still inside the stale window.

    Args:
        root: Directory to walk. Missing directories yield nothing.
        pattern: Glob such as *.jsonl.
        now: Current Unix time.
        stale_seconds: Maximum age to treat as a live session.

    Returns:
        Up to PRIME_LIMIT (mtime, path) pairs, newest first.
    """
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob(pattern):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if now - modified <= stale_seconds:
            found.append((modified, path))
    found.sort(reverse=True)
    return found[:PRIME_LIMIT]


def discover_sessions(
        home,
        now,
        stale_seconds,
):
    """Tiles for sessions that were already open before piDeck started.

    Args:
        home: Home directory containing .claude, .codex, and .cursor.
        now: Current Unix time.
        stale_seconds: Ignore files older than this.

    Returns:
        Lifecycle events with a project name and idle or working state. No transcript text.
    """
    events = []
    for modified, path in _recent_files(home / ".claude" / "projects", "*.jsonl", now, stale_seconds):
        if "subagents" in path.parts:
            continue
        fields = _header_fields(path, ("sessionId", "cwd"))
        session_id = fields.get("sessionId") or path.stem
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
            continue
        cwd = fields.get("cwd")
        events.append({
            "session_id": session_id,
            "state": "working" if now - modified < WORKING_AGE_SECONDS else "idle",
            "agent": "claude",
            "cwd": cwd,
            "name": Path(cwd).name if cwd else path.parent.name.split("-")[-1],
        })
    newest_codex = None
    for modified, path in _recent_files(home / ".codex" / "sessions", "*.jsonl", now, stale_seconds):
        fields = _header_fields(path, ("session_id", "cwd"))
        named = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", path.name)
        session_id = fields.get("session_id") or (named.group(0) if named else None)
        if not session_id:
            continue
        cwd = fields.get("cwd")
        event = {
            "session_id": session_id,
            "state": "working" if now - modified < WORKING_AGE_SECONDS else "idle",
            "agent": "codex",
            "cwd": cwd,
            "name": Path(cwd).name if cwd else "codex",
        }
        if newest_codex is None:
            newest_codex = path
            event.update(codex_metrics({"transcript_path": str(path), "session_id": session_id}, {}))
        events.append(event)
    cursor_root = home / ".cursor" / "projects"
    if cursor_root.is_dir():
        cursor_files = []
        for project in cursor_root.iterdir():
            transcripts = project / "agent-transcripts"
            if not transcripts.is_dir():
                continue
            for modified, path in _recent_files(transcripts, "*.jsonl", now, stale_seconds):
                cursor_files.append((modified, path, project.name.split("-")[-1] or "cursor"))
        cursor_files.sort(reverse=True)
        for modified, path, name in cursor_files[:PRIME_LIMIT]:
            session_id = path.parent.name
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
                continue
            events.append({
                "session_id": "cursor:" + session_id,
                "state": "working" if now - modified < WORKING_AGE_SECONDS else "idle",
                "agent": "cursor",
                "name": name,
            })
    return events


def prime_events(
        env,
        now=None,
        opener=None,
):
    """Usage samples and already-open sessions to publish when piDeck starts.

    Args:
        env: Environment mapping for home, cache, and the Cursor sign-in.
        now: Current Unix time, or None to use the clock.
        opener: Optional urlopen replacement for the Cursor usage request.

    Returns:
        Events safe to post: percentages, project names, and states. No prompts.
    """
    now = time.time() if now is None else now
    home = Path(env.get("PIDECK_HOME") or Path.home())
    stale = float(env.get("PIDECK_PRIME_HOURS") or 6) * 3600
    events = cached_usage_events(env)
    events.extend(discover_sessions(home, now, stale))
    cursor = fetch_cursor_usage(env, opener)
    if cursor:
        events.append(cursor)
    return events


def post(ev, env):
    remember_usage(ev, env)
    conf = load_settings(env)
    headers = {"Content-Type": "application/json"}
    if conf["token"]:
        headers["Authorization"] = "Bearer " + conf["token"]
    url = conf["url"].rstrip("/") + "/api/agents/event"
    urllib.request.urlopen(urllib.request.Request(url, json.dumps(ev).encode(), headers), timeout=2).read()


def main():
    source = "claude"
    cursor_response = {}
    try:
        if "--prime" in sys.argv[1:]:
            for ev in prime_events(os.environ):
                try:
                    post(ev, os.environ)
                except Exception:
                    pass
            return 0
        if "--statusline" in sys.argv[1:]:
            run_statusline(sys.stdin.read(), os.environ)
            return 0
        source, notify = parse_args(sys.argv[1:])
        if notify is not None:
            ev = build_notify_event(json.loads(notify), os.environ)
        else:
            payload = json.loads(sys.stdin.buffer.read())
            if source == "cursor":
                cursor_response = cursor_hook_response(payload.get("hook_event_name"))
            ev = build_event(payload, os.environ, source)
        if ev:
            post(ev, os.environ)
    except Exception:
        pass  # the display being offline must never affect the agent
    finally:
        if source == "cursor":
            # Cursor permission hooks require a valid response, even if piDeck is offline.
            print(json.dumps(cursor_response))
    return 0  # and print nothing: Codex treats stray stdout from a Stop hook as a failure


if __name__ == "__main__":
    sys.exit(main())
