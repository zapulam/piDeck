"""

Claude Code / Codex / Cursor hook -> piDeck. Stdlib only and never allowed to break your session.

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
  PIDECK_URL             default http://localhost:8765; remote addresses must use HTTPS
  PIDECK_HOOK_TOKEN      event-only credential; never use the display administrator token
  PIDECK_CONFIG          path to a different hook.json
  PIDECK_NAME            tile label override (default: the project folder name)
  PIDECK_CONTEXT_WINDOW  tokens for the ctx% estimate (default 200000; Claude Code only)
  CODEX_HOME             Codex data directory (default ~/.codex), used to find session metrics
  PIDECK_SEND_MESSAGES   set to 1 to also send a 200-character snippet of the assistant's last reply
                         (off by default: it crosses your network, and the server may be plain HTTP).
                         Or set "send_messages": true in ~/.config/pideck/hook.json.

"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Mapping

import json
import csv
import io
import ipaddress
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from functools import lru_cache
from urllib.parse import urlsplit
import urllib.error
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


# --- Private files and authenticated transport ---


@lru_cache(maxsize=1)
def windows_user_sid() -> str:
    """
    Resolve the current Windows user's SID without relying on localized account names.

    Returns:
        str: The current user's security identifier.
    """
    result = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, check=True, creationflags=0x08000000)
    return next(csv.reader(io.StringIO(result.stdout.strip())))[1]


def protect_file(
    path: Path,
    ) -> None:
    """
    Restrict an existing private file to its owner and the operating system.

    Args:
        path (Path): Secret or private state file whose permissions must be restricted.

    Returns:
        None: Permissions are applied, or an exception prevents unsafe persistence.
    """
    if os.name == "nt":
        subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                        f"*{windows_user_sid()}:F", "*S-1-5-18:F"],
                       capture_output=True, check=True, creationflags=0x08000000)
    else:
        path.chmod(0o600)


def private_write(
    path: Path,
    text: str,
    ) -> None:
    """
    Atomically replace a private UTF-8 file, restricting access before writing secrets.

    Args:
        path (Path): Destination beneath a user-owned configuration directory.
        text (str): Contents to persist with LF newlines.

    Returns:
        None: The private destination contains the supplied text.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pideck-", dir=path.parent)
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            protect_file(staging)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def validate_transport_url(
    url: str,
    ) -> str:
    """
    Require HTTPS outside literal loopback addresses and reject URL credentials.

    Args:
        url (str): Intended server or provider URL, without embedded secrets.

    Returns:
        str: The unchanged validated URL.
    """
    parsed = urlsplit(url)
    if parsed.username or parsed.password or not parsed.hostname or parsed.fragment:
        raise ValueError("Use an HTTPS address without credentials or fragments")
    host = parsed.hostname.lower()
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Remote piDeck connections require HTTPS; use an encrypted HTTPS tunnel")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirects so bearer credentials never reach another endpoint."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
        ) -> None:
        """
        Refuse every redirect, including redirects to the same origin.

        Args:
            req (urllib.request.Request): Original authenticated request.
            fp (Any): Original response stream.
            code (int): Redirect status code.
            msg (str): Response status message.
            headers (Any): Original response headers.
            newurl (str): Proposed redirect destination, which is never visited.

        Returns:
            None: urllib raises its normal HTTPError without forwarding credentials.
        """
        return None


def safe_open(
    request: urllib.request.Request,
    timeout: float = 5,
    ) -> Any:
    """
    Open a validated request with certificate verification and redirects disabled.

    Args:
        request (urllib.request.Request): Request for its intended HTTPS or loopback endpoint.
        timeout (float): Maximum blocking socket timeout in seconds.

    Returns:
        Any: urllib response owned by the caller.
    """
    validate_transport_url(request.full_url)
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


# --- Usage helpers ---


def transcript_records(
    transcript_path: str | Path | None,
    ) -> Iterator[dict[str, Any]]:
    """
    Yield recent JSON objects newest first, tolerating missing or truncated logs.

    Args:
        transcript_path (str | Path | None): Local session transcript to read, capped at TAIL_BYTES.

    Returns:
        Iterator[dict[str, Any]]: Newest-first iterator over valid JSON object records in the
            bounded transcript tail.

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
    value: Any,
    ) -> float | None:
    """
    Convert a provider reset time to Unix seconds.

    Args:
        value (Any): Unix seconds, milliseconds, or an ISO-8601 timestamp.

    Returns:
        float | None: A non-negative Unix timestamp, or None when the value is absent or unusable.
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
    data: Any,
    percent_key: str,
    minutes: Any,
    ) -> dict[str, Any] | None:
    """
    Normalize one provider window, returning None for absent or invalid metrics.

    Args:
        data (Any): Provider window dictionary, or None when unavailable.
        percent_key (str): Provider-specific key containing percent used.
        minutes (Any): Duration of the allowance window in minutes.

    Returns:
        dict[str, Any] | None: Only the numeric fields accepted by piDeck, without any account
            credentials.
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
    payload: dict[str, Any],
    env: Mapping[str, str],
    ) -> dict[str, Any]:
    """
    Read context and account usage from this Codex session's latest token events.

    Args:
        payload (dict[str, Any]): Hook or notify payload, optionally containing transcript_path.
        env (Mapping[str, str]): Environment mapping; CODEX_HOME overrides the default ~/.codex
            directory.

    Returns:
        dict[str, Any]: Available context_pct and usage fields. Missing data is never treated as
            zero.
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


def context_pct(
    transcript_path: str | Path,
    window: float,
    ) -> float | None:
    """
    Best-effort (Claude Code transcripts): latest assistant prompt tokens / window. None if unknown.

    Args:
        transcript_path (str | Path): Local session transcript to inspect for recent context usage.
        window (float): Token capacity used as the context-percentage denominator.

    Returns:
        float | None: Estimated context-used percentage capped at 100, or None when transcript usage
            is unavailable.
    """
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


def _saved(
    env: Mapping[str, str],
    ) -> Any:
    """
    Read saved hook settings while tolerating absent or invalid files.

    Args:
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        Any: Decoded saved settings, or an empty dictionary when reading or parsing fails.
    """
    path = env.get("PIDECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "pideck", "hook.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def wants_messages(
    env: Mapping[str, str],
    ) -> bool:
    """
    Resolve whether the user has opted in to forwarding assistant reply snippets.

    Args:
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        bool: True when environment settings or saved preferences enable reply snippets.
    """
    v = env.get("PIDECK_SEND_MESSAGES")
    return v.lower() in ("1", "true", "yes") if v is not None else bool(_saved(env).get("send_messages"))


# --- Event builders ---


def build_event(
    payload: dict[str, Any],
    env: Mapping[str, str],
    source: str = 'claude',
    ) -> dict[str, Any] | None:
    """
    Build a lifecycle event with optional context and account-usage metrics.

    Args:
        payload (dict[str, Any]): Claude Code or Codex lifecycle hook input.
        env (Mapping[str, str]): Hook environment controlling display names, messages, and local
            paths.
        source (str): Provider identifier, normally claude or codex.

    Returns:
        dict[str, Any] | None: A piDeck event containing only the selected display fields and
            metrics.
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
    msg = (payload.get("message") or payload.get("last_assistant_message")) if wants_messages(env) else None
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
    value: Any,
    limit: int = 60,
    ) -> str:
    """
    Keep an identifier short and free of whitespace.

    Args:
        value (Any): Raw id from a hook payload or a file name.
        limit (int): Maximum characters to keep.

    Returns:
        str: A single-line identifier, or an empty string.
    """
    return "".join(str(value or "").split())[:limit]


def cursor_subagent_ids(
    payload: dict[str, Any],
    ) -> tuple[str | None, str | None]:
    """
    Session and parent ids for a Cursor subagent, without its task text.

    Args:
        payload (dict[str, Any]): subagentStart or subagentStop input.

    Returns:
        tuple[str | None, str | None]: (session_id, parent_id). Either is None when the payload has
            no identity.
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
    event: str | None,
    ) -> dict[str, Any]:
    """
    The JSON Cursor requires before it will continue a permission hook.

    Args:
        event (str | None): Cursor hook_event_name.

    Returns:
        dict[str, Any]: A schema-valid response. subagentStart must allow, or Cursor blocks the
            child.
    """
    if event == "beforeSubmitPrompt":
        return {"continue": True}
    if event in ("preToolUse", "subagentStart"):
        return {"permission": "allow"}
    return {}


def cursor_detail(
    payload: dict[str, Any],
    ) -> str | None:
    """
    A short public label for one Cursor chat, never a prompt or account identity.

    Args:
        payload (dict[str, Any]): Cursor hook input. Only the composer mode and model slug are read.

    Returns:
        str | None: Ask or edit mode plus the model slug, trimmed to 64 characters, or None.
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
    payload: dict[str, Any],
    env: Mapping[str, str],
    ) -> dict[str, Any] | None:
    """
    Translate Cursor hooks without forwarding prompts, tool inputs, or account identity.

    Args:
        payload (dict[str, Any]): Cursor's documented hook input, keyed by conversation_id.
        env (Mapping[str, str]): Environment mapping supporting the PIDECK_NAME tile label override.

    Returns:
        dict[str, Any] | None: A piDeck lifecycle or context-only event, or None for unrelated
            hooks. Context is cleared on later activity because preCompact is only a snapshot.
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


def build_notify_event(
    payload: dict[str, Any],
    env: Mapping[str, str],
    ) -> dict[str, Any] | None:
    """
    Codex `notify` payload (kebab-case keys). Only 'agent-turn-complete' exists today -> done.

    Args:
        payload (dict[str, Any]): Decoded provider payload to inspect or translate.
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        dict[str, Any] | None: Done-state agent update for a supported Codex completion event,
            otherwise None.
    """
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


def statusline_context(
    payload: dict[str, Any],
    ) -> float | None:
    """
    Context percent from Claude's status line, including the token fallback.

    Args:
        payload (dict[str, Any]): Claude Code status-line JSON.

    Returns:
        float | None: A percentage from 0 to 100, or None before the first model response.
    """
    window = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    pct = window.get("used_percentage")
    if isinstance(pct, (int, float)) and math.isfinite(pct) and 0 <= pct <= 100:
        return pct
    usage = window.get("current_usage") if isinstance(window.get("current_usage"), dict) else None
    size = window.get("context_window_size")
    if usage is None or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        return None
    try:
        used = sum(float(usage.get(key) or 0) for key in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(used) or used < 0:
        return None
    return round(min(100.0, used * 100.0 / float(size)), 1)


def claude_context_window(
    path: str | Path | None,
    ) -> int:
    """
    Read the context-window size recorded in a Claude transcript.

    Args:
        path (str | Path | None): Session JSONL file. Only the trailing bytes are scanned.

    Returns:
        int: The recorded window, or 200000 when the file does not name one.
    """
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - TAIL_BYTES))
            raw = stream.read().decode("utf-8", "ignore")
    except OSError:
        return 200000
    match = re.search(r'"(?:context_window_size|contextWindow)"\s*:\s*(\d+)', raw)
    if not match:
        return 200000
    size = int(match.group(1))
    return size if 1000 <= size <= 2_000_000 else 200000


def build_statusline_event(
    payload: dict[str, Any],
    ) -> dict[str, Any]:
    """
    Convert Claude's documented status-line metrics without changing session state.

    Args:
        payload (dict[str, Any]): Claude Code status-line JSON received on stdin.

    Returns:
        dict[str, Any]: A usage-only event containing only context, account percentages, and
            identity.
    """
    limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    ev = {"session_id": payload.get("session_id") or "unknown", "event": "UsageUpdate", "agent": "claude"}
    pct = statusline_context(payload)
    if pct is not None:
        ev["context_pct"] = pct
    primary = usage_window(limits.get("five_hour"), "used_percentage", 300)
    secondary = usage_window(limits.get("seven_day"), "used_percentage", 10080)
    if primary or secondary:
        if primary:
            primary["label"] = "5h"
        if secondary:
            secondary["label"] = "Weekly"
        ev["usage"] = {"primary": primary, "secondary": secondary}
    return ev


def run_statusline(
    raw: str,
    env: Mapping[str, str],
    ) -> None:
    """
    Forward Claude metrics and retain the user's existing status-line output.

    Args:
        raw (str): Unmodified JSON input, also passed to the original status-line command.
        env (Mapping[str, str]): Environment mapping used to load piDeck's settings.

    Returns:
        None: No value is returned.
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


# --- Hook transport and entry point ---


def parse_args(
    argv: list[str],
    ) -> tuple[str, str | None]:
    """
    Parse the provider flag and detect a legacy Codex notify payload.

    Args:
        argv (list[str]): Command-line arguments, or None to read the current process arguments.

    Returns:
        tuple[str, str | None]: Selected provider and optional JSON notify payload supplied on the
            command line.
    """
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


def load_settings(
    env: Mapping[str, str],
    ) -> dict[str, str]:
    """
    url/token: environment wins, then the file the installer wrote, then defaults.
    The file matters because apps started from a dock or Start menu don't see shell exports.

    Args:
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        dict[str, str]: Display URL and token resolved from environment, saved settings, then
            defaults.
    """
    saved = _saved(env)
    return {
        "url": env.get("PIDECK_URL") or saved.get("url") or "http://localhost:8765",
        "token": env.get("PIDECK_HOOK_TOKEN") or saved.get("token") or "",
    }


# --- Cached allowance samples ---


def usage_cache_path(
    env: Mapping[str, str],
    ) -> Path:
    """
    Where the last account-usage sample is kept so a later launch can show it.

    Args:
        env (Mapping[str, str]): Environment mapping. PIDECK_CONFIG selects a different hook
            directory.

    Returns:
        Path: The cache path beside hook.json. The file holds percentages only.
    """
    saved = env.get("PIDECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "pideck", "hook.json")
    return Path(saved).parent / "usage-cache.json"


def remember_usage(
    ev: dict[str, Any],
    env: Mapping[str, str],
    ) -> None:
    """
    Store one provider's allowance sample for the next piDeck launch.

    Args:
        ev (dict[str, Any]): Event that may contain an agent name and a usage object.
        env (Mapping[str, str]): Environment mapping used to locate the cache.

    Returns:
        None: No value is returned.
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
        previous = current.get(agent) if isinstance(current.get(agent), dict) else {}
        entry = {"usage": usage, "updated_at": time.time()}
        # Replaying a cached sample must not look like a fresh provider fetch.
        if str(ev.get("session_id") or "").startswith("usage:"):
            if previous.get("fetched_at"):
                entry["fetched_at"] = previous["fetched_at"]
        else:
            entry["fetched_at"] = entry["updated_at"]
        current[agent] = entry
        path.write_text(json.dumps(current), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except (OSError, ValueError):
        return


def cached_usage_events(
    env: Mapping[str, str],
    ) -> list[dict[str, Any]]:
    """
    Turn the saved allowance sample into usage-only events.

    Args:
        env (Mapping[str, str]): Environment mapping used to locate the cache.

    Returns:
        list[dict[str, Any]]: One UsageUpdate per cached provider. An unreadable cache yields
            nothing.
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


def _load_cache(
    env: Mapping[str, str],
    ) -> dict[str, Any]:
    """
    Read the percentage cache, or an empty object when it is missing.

    Args:
        env (Mapping[str, str]): Environment mapping used to locate the cache.

    Returns:
        dict[str, Any]: The cache object. A missing or unreadable file yields an empty dict.
    """
    try:
        data = json.loads(usage_cache_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stamp_fetch(
    env: Mapping[str, str],
    agent: str,
    when: float,
    ) -> None:
    """
    Remember when a provider was last asked, even if the answer was unusable.

    Args:
        env (Mapping[str, str]): Environment mapping used to locate the cache.
        agent (str): Provider name, one of claude, codex, or cursor.
        when (float): Unix time of the attempt.

    Returns:
        None: No value is returned.
    """
    path = usage_cache_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _load_cache(env)
        entry = dict(current.get(agent) if isinstance(current.get(agent), dict) else {})
        entry["fetched_at"] = when
        current[agent] = entry
        path.write_text(json.dumps(current), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return


def note_fetch(
    env: Mapping[str, str],
    agent: str,
    note: str,
    ) -> None:
    """
    Record why an account-usage request produced no bars. Never store a token or body.

    Args:
        env (Mapping[str, str]): Environment mapping used to locate the cache.
        agent (str): Provider name.
        note (str): Short status such as http_401 or no_sign_in.

    Returns:
        None: No value is returned.
    """
    path = usage_cache_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _load_cache(env)
        entry = dict(current.get(agent) if isinstance(current.get(agent), dict) else {})
        entry["note"] = str(note)[:40]
        current[agent] = entry
        path.write_text(json.dumps(current), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return


def fetched_at(
    env: Mapping[str, str],
    agent: str,
    ) -> float:
    """
    When this provider was last asked for account usage.

    Args:
        env (Mapping[str, str]): Environment mapping used to locate the cache.
        agent (str): Provider name.

    Returns:
        float: A Unix time, or 0 when this provider has not been asked.
    """
    entry = _load_cache(env).get(agent)
    if not isinstance(entry, dict):
        return 0
    try:
        return float(entry.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return 0


# --- Provider allowance normalization ---


def _window(
    percent: Any,
    minutes: int,
    resets_at: Any,
    label: str,
    ) -> dict[str, Any] | None:
    """
    Build one allowance window, or None when the percent is unusable.

    Args:
        percent (Any): Percent used, already numeric.
        minutes (int): Window length in minutes.
        resets_at (Any): Unix reset time, or None.
        label (str): Short bar caption such as Included or API.

    Returns:
        dict[str, Any] | None: A piDeck usage window, or None.
    """
    window = usage_window({"used_percentage": percent, "resets_at": resets_at}, "used_percentage", minutes)
    if window and label:
        window["label"] = label
    return window


def _field(
    source: Any,
    *names: str,
    ) -> Any:
    """
    Return the first present value among several spellings of one field.

    Args:
        source (Any): Object that may hold the field.
        *names (str): Candidate keys, preferred name first.

    Returns:
        Any: First present non-null value matching a supplied field name, otherwise None.
    """
    if not isinstance(source, dict):
        return None
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return None


def cursor_usage_from_body(
    payload: Any,
    ) -> dict[str, Any] | None:
    """
    Read Cursor's current-period usage payload down to two public bars.

    Args:
        payload (Any): JSON object from DashboardService/GetCurrentPeriodUsage.

    Returns:
        dict[str, Any] | None: A usage object with Included and API windows, or None when no percent
            is present.
    """
    if not isinstance(payload, dict):
        return None
    plan = _field(payload, "planUsage", "plan_usage")
    if not isinstance(plan, dict):
        individual = _field(payload, "individualUsage", "individual_usage")
        plan = _field(individual, "plan") if isinstance(individual, dict) else None
    if not isinstance(plan, dict):
        plan = {}
    reset = _field(payload, "billingCycleEnd", "billing_cycle_end")
    # The dashboard sentence "N% of your included usage" is included spend over the plan limit.
    # totalPercentUsed is a different internal mix and is only a fallback.
    limit = _field(plan, "limit")
    spent = _field(plan, "includedSpend", "included_spend")
    if isinstance(limit, (int, float)) and isinstance(spent, (int, float)) and limit > 0 and spent >= 0:
        included = min(100.0, spent * 100.0 / limit)
    else:
        included = _field(plan, "totalPercentUsed", "total_percent_used")
    api = _field(plan, "apiPercentUsed", "api_percent_used")
    auto = _field(plan, "autoPercentUsed", "auto_percent_used")
    primary = _window(included if included is not None else auto, 43200, reset, "Included")
    secondary_pct = api if api is not None else (auto if included is not None else None)
    secondary = _window(secondary_pct, 43200, reset, "API" if api is not None else "Auto")
    if primary is None and secondary is None:
        return None
    return {"primary": primary, "secondary": secondary}


# --- Cursor sign-in and allowance requests ---


def cursor_state_db(
    env: Mapping[str, str],
    ) -> Path:
    """
    Locate Cursor's local state database without opening it.

    Args:
        env (Mapping[str, str]): Environment mapping. CURSOR_STATE_DB overrides the platform path.

    Returns:
        Path: Path to state.vscdb.
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


def read_cursor_value(
    env: Mapping[str, str],
    key: str,
    ) -> str | None:
    """
    Read one Cursor sign-in value. Callers must not log or forward it.

    Args:
        env (Mapping[str, str]): Environment mapping used to find state.vscdb.
        key (str): ItemTable key, such as cursorAuth/accessToken.

    Returns:
        str | None: The stored string, or None when the database or the key is unavailable.
    """
    path = cursor_state_db(env)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "select value from ItemTable where key = ? limit 1",
                (key,),
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


def read_cursor_token(
    env: Mapping[str, str],
    ) -> str | None:
    """
    Read the local Cursor access token. Callers must not log or forward it.

    Args:
        env (Mapping[str, str]): Environment mapping used to find state.vscdb.

    Returns:
        str | None: The access token, or None when the database or the key is unavailable.
    """
    return read_cursor_value(env, "cursorAuth/accessToken")


# Cursor's desktop access token expires after about an hour. This is the public client id it refreshes with.
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"


def refresh_cursor_token(
    env: Mapping[str, str],
    opener: Callable[..., Any] | None,
    ) -> tuple[str | None, str]:
    """
    Exchange Cursor's refresh token for a new access token. Nothing is written back.

    Args:
        env (Mapping[str, str]): Environment mapping used to find state.vscdb.
        opener (Callable[..., Any] | None): Callable matching urllib.request.urlopen.

    Returns:
        tuple[str | None, str]: The new access token and an empty note, or None and a short reason.
    """
    refresh = read_cursor_value(env, "cursorAuth/refreshToken")
    if not refresh:
        return None, "no_refresh"
    request = urllib.request.Request(
        "https://api2.cursor.sh/oauth/token",
        data=json.dumps({
            "grant_type": "refresh_token",
            "client_id": CURSOR_CLIENT_ID,
            "refresh_token": refresh,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        return None, "refresh_http_" + str(exc.code)
    except (OSError, ValueError) as exc:
        return None, "refresh_bad_json" if isinstance(exc, ValueError) else "refresh_unreachable"
    if not isinstance(body, dict) or body.get("shouldLogout") is True:
        return None, "refresh_logout"
    token = body.get("access_token") or body.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "refresh_empty"
    return token, ""


def _cursor_usage_request(
    token: str,
    ) -> urllib.request.Request:
    """
    Build the current-period usage request. The token stays in the header only.

    Args:
        token (str): Cursor access token.

    Returns:
        urllib.request.Request: The POST request.
    """
    return urllib.request.Request(
        "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
        data=b"{}",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )


def fetch_cursor_usage(
    env: Mapping[str, str],
    opener: Callable[..., Any] | None = None,
    ) -> dict[str, Any] | None:
    """
    Ask Cursor's usage service for percentages. The token is not included in the result.

    Args:
        env (Mapping[str, str]): Environment mapping used to find the local sign-in.
        opener (Callable[..., Any] | None): Callable matching urllib.request.urlopen, for tests.

    Returns:
        dict[str, Any] | None: A usage-only Cursor event, or None when the sign-in or the service is
            unavailable.
    """
    open_url = opener or safe_open
    token = read_cursor_token(env)
    if not token:
        note_fetch(env, "cursor", "no_sign_in")
        return None
    request = _cursor_usage_request(token)
    try:
        with open_url(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            note_fetch(env, "cursor", "http_" + str(exc.code))
            return None
        token, reason = refresh_cursor_token(env, open_url)
        if not token:
            note_fetch(env, "cursor", reason)
            return None
        request = _cursor_usage_request(token)
        try:
            with open_url(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as retry:
            note_fetch(env, "cursor", "http_" + str(retry.code))
            return None
        except (OSError, ValueError) as retry:
            note_fetch(env, "cursor", "bad_json" if isinstance(retry, ValueError) else type(retry).__name__)
            return None
    except (OSError, ValueError) as exc:
        note_fetch(env, "cursor", "bad_json" if isinstance(exc, ValueError) else type(exc).__name__)
        return None
    usage = cursor_usage_from_body(body)
    if not usage:
        note_fetch(env, "cursor", "unrecognized")
        return None
    event = {"session_id": "usage:cursor", "event": "UsageUpdate", "agent": "cursor", "usage": usage}
    remember_usage(event, env)
    return event


# Claude's usage endpoint rate-limits a token after a short burst, so a launch asks at most twice an hour.
CLAUDE_USAGE_INTERVAL = 1800


# --- Claude sign-in and allowance requests ---


def claude_credentials_path(
    env: Mapping[str, str],
    ) -> Path:
    """
    Locate the Claude sign-in file without opening it.

    Args:
        env (Mapping[str, str]): Environment mapping. CLAUDE_CREDENTIALS overrides the home path.

    Returns:
        Path: Path to the credentials file.
    """
    override = env.get("CLAUDE_CREDENTIALS")
    if override:
        return Path(override)
    home = Path(env.get("PIDECK_HOME") or Path.home())
    return home / ".claude" / ".credentials.json"


def read_claude_token(
    env: Mapping[str, str],
    ) -> str | None:
    """
    Read the local Claude access token. Callers must not log or forward it.

    Args:
        env (Mapping[str, str]): Environment mapping used to find the credentials file.

    Returns:
        str | None: The access token, or None when the file or the sign-in is unavailable.
    """
    path = claude_credentials_path(env)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    token = oauth.get("accessToken") or oauth.get("access_token")
    if isinstance(token, str) and token:
        return token
    fallback = env.get("CLAUDE_CODE_OAUTH_TOKEN")
    return fallback if isinstance(fallback, str) and fallback else None


def claude_sign_in_note(
    env: Mapping[str, str],
    ) -> str:
    """
    Why the Claude sign-in could not be read, without including its contents.

    Args:
        env (Mapping[str, str]): Environment mapping used to find the credentials file.

    Returns:
        str: A short note such as missing_file, bad_json, or the key names that were present.
    """
    path = claude_credentials_path(env)
    if not path.is_file():
        return "missing_file"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return "unreadable"
    except ValueError:
        return "bad_json"
    if not isinstance(data, dict):
        return "bad_json"
    oauth = data.get("claudeAiOauth")
    source = oauth if isinstance(oauth, dict) else data
    token = source.get("accessToken") if isinstance(source, dict) else None
    if token is None:
        kind = "missing"
    elif isinstance(token, str):
        kind = "empty" if not token else "str"
    else:
        kind = type(token).__name__
    return ("token_" + kind)[:40]


def claude_usage_from_body(
    payload: Any,
    ) -> dict[str, Any] | None:
    """
    Read Claude's 5-hour and weekly utilization down to two public bars.

    Args:
        payload (Any): JSON object with five_hour and seven_day utilization.

    Returns:
        dict[str, Any] | None: A usage object with 5h and Weekly windows, or None when both are
            missing.
    """
    if not isinstance(payload, dict):
        return None
    primary = usage_window(payload.get("five_hour"), "utilization", 300)
    secondary = usage_window(payload.get("seven_day"), "utilization", 10080)
    if primary:
        primary["label"] = "5h"
    if secondary:
        secondary["label"] = "Weekly"
    if primary is None and secondary is None:
        return None
    return {"primary": primary, "secondary": secondary}


def fetch_claude_usage(
    env: Mapping[str, str],
    opener: Callable[..., Any] | None = None,
    now: float | None = None,
    ) -> dict[str, Any] | None:
    """
    Ask Claude for allowance percents. The token is not included in the result.

    Args:
        env (Mapping[str, str]): Environment mapping used to find the local sign-in and the cache.
        opener (Callable[..., Any] | None): Callable matching urllib.request.urlopen, for tests.
        now (float | None): Current Unix time, or None to use the clock.

    Returns:
        dict[str, Any] | None: A usage-only Claude event, or None when a recent sample exists or the
            service is unavailable.
    """
    now = time.time() if now is None else now
    if now - fetched_at(env, "claude") < CLAUDE_USAGE_INTERVAL:
        return None
    stamp_fetch(env, "claude", now)
    token = read_claude_token(env)
    if not token:
        note_fetch(env, "claude", claude_sign_in_note(env))
        return None
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with (opener or safe_open)(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        note_fetch(env, "claude", "http_" + str(exc.code))
        return None
    except (OSError, ValueError) as exc:
        note_fetch(env, "claude", "bad_json" if isinstance(exc, ValueError) else type(exc).__name__)
        return None
    usage = claude_usage_from_body(body)
    if not usage:
        note_fetch(env, "claude", "unrecognized")
        return None
    event = {"session_id": "usage:claude", "event": "UsageUpdate", "agent": "claude", "usage": usage}
    remember_usage(event, env)
    return event


# --- Startup session discovery ---


def _header_fields(
    path: Path,
    wanted: tuple[str, ...],
    ) -> dict[str, str]:
    """
    Read a few named fields from the start of a session file, never the message body.

    Args:
        path (Path): JSONL session file.
        wanted (tuple[str, ...]): Field names to copy when present on an object.

    Returns:
        dict[str, str]: The first values found for those names.
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
    root: Path,
    pattern: str,
    now: float,
    stale_seconds: float,
    ) -> list[tuple[float, Path]]:
    """
    Newest files under root whose mtime is still inside the stale window.

    Args:
        root (Path): Directory to walk. Missing directories yield nothing.
        pattern (str): Glob such as *.jsonl.
        now (float): Current Unix time.
        stale_seconds (float): Maximum age to treat as a live session.

    Returns:
        list[tuple[float, Path]]: Up to PRIME_LIMIT (mtime, path) pairs, newest first.
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
    home: Path,
    now: float,
    stale_seconds: float,
    ) -> list[dict[str, Any]]:
    """
    Tiles for sessions that were already open before piDeck started.

    Args:
        home (Path): Home directory containing .claude, .codex, and .cursor.
        now (float): Current Unix time.
        stale_seconds (float): Ignore files older than this.

    Returns:
        list[dict[str, Any]]: Lifecycle events with a project name and idle or working state. No
            transcript text.
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
        event = {
            "session_id": session_id,
            "state": "working" if now - modified < WORKING_AGE_SECONDS else "idle",
            "agent": "claude",
            "cwd": cwd,
            "name": Path(cwd).name if cwd else path.parent.name.split("-")[-1],
        }
        pct = context_pct(str(path), claude_context_window(path))
        if pct is not None:
            event["context_pct"] = pct
        events.append(event)
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
    env: Mapping[str, str],
    now: float | None = None,
    opener: Callable[..., Any] | None = None,
    ) -> list[dict[str, Any]]:
    """
    Usage samples and already-open sessions to publish when piDeck starts.

    Args:
        env (Mapping[str, str]): Environment mapping for home, cache, and the local sign-ins.
        now (float | None): Current Unix time, or None to use the clock.
        opener (Callable[..., Any] | None): Optional urlopen replacement for the Claude and Cursor
            usage requests.

    Returns:
        list[dict[str, Any]]: Events safe to post: percentages, project names, and states. No
            prompts.
    """
    now = time.time() if now is None else now
    home = Path(env.get("PIDECK_HOME") or Path.home())
    stale = float(env.get("PIDECK_PRIME_HOURS") or 6) * 3600
    events = cached_usage_events(env)
    events.extend(discover_sessions(home, now, stale))
    claude = fetch_claude_usage(env, opener, now)
    if claude:
        events.append(claude)
    cursor = fetch_cursor_usage(env, opener)
    if cursor:
        events.append(cursor)
    return events


# --- Event delivery and command-line entry point ---


def post(
    ev: dict[str, Any],
    env: Mapping[str, str],
    ) -> None:
    """
    Cache permitted usage values and post one agent event to the configured display.

    Args:
        ev (dict[str, Any]): Agent event containing the session identifier and permitted update
            fields.
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        None: No value is returned.
    """
    remember_usage(ev, env)
    conf = load_settings(env)
    ev = dict(ev)
    if ev.get("cwd") and env.get("PIDECK_SEND_PATHS") != "1":
        name = str(ev.pop("cwd")).replace("\\", "/").rstrip("/").split("/")[-1]
        ev.setdefault("name", name[:64])
    if not wants_messages(env):
        ev.pop("message", None)
    headers = {"Content-Type": "application/json"}
    if conf["token"]:
        headers["Authorization"] = "Bearer " + conf["token"]
    url = conf["url"].rstrip("/") + "/api/agents/event"
    with safe_open(urllib.request.Request(url, json.dumps(ev).encode(), headers), timeout=2) as response:
        response.read(65536)


def main() -> int:
    """
    Dispatch lifecycle, notify, status-line, or startup updates without breaking the agent.

    Returns:
        int: Zero even when input, provider access, or display delivery fails, so the agent can
            continue.
    """
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
