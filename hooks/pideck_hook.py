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
import subprocess
import sys
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
}


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
    event = CURSOR_EVENTS.get(payload.get("hook_event_name"))
    sid = payload.get("conversation_id") or payload.get("session_id")
    if event is None or not sid:
        return None
    roots = payload.get("workspace_roots") or []
    ev = {
        "session_id": "cursor:" + str(sid),
        "event": event,
        "agent": "cursor",
        "cwd": payload.get("cwd") or (roots[0] if roots else None),
        "context_pct": None,
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
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


def post(ev, env):
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
        if "--statusline" in sys.argv[1:]:
            run_statusline(sys.stdin.read(), os.environ)
            return 0
        source, notify = parse_args(sys.argv[1:])
        if notify is not None:
            ev = build_notify_event(json.loads(notify), os.environ)
        else:
            payload = json.loads(sys.stdin.buffer.read())
            if source == "cursor":
                event = payload.get("hook_event_name")
                if event == "beforeSubmitPrompt":
                    cursor_response = {"continue": True}
                elif event == "preToolUse":
                    cursor_response = {"permission": "allow"}
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
