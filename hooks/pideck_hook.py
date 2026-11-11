"""

Claude Code / Codex / Cursor hook -> piDeck. Stdlib only and never allowed to break your session.

Supported inputs:

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
  CODEX_HOME             Codex data directory (default ~/.codex), used to find session metrics
  PIDECK_SEND_MESSAGES   set to 1 to also send a 200-character snippet of the assistant's last reply
                         (off by default; remote authenticated displays require HTTPS).
                         Or set "send_messages": true in ~/.config/pideck/hook.json.

"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

import json
import csv
import io
import ipaddress
import math
import hashlib
import os
import re
import subprocess
import sys
import tempfile
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
    "postToolUseFailure": "PostToolUseFailure",
    "stop": "Stop",
    "sessionEnd": "SessionEnd",
    "subagentStart": "UserPromptSubmit",
    "subagentStop": "Stop",
}
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
    Read context from the latest reported Codex token event; account limits use app-server.

    Args:
        payload (dict[str, Any]): Hook or notify payload, optionally containing transcript_path.
        env (Mapping[str, str]): Environment mapping; CODEX_HOME overrides the default ~/.codex
            directory.

    Returns:
        dict[str, Any]: Available context percentage. Missing data is never treated as zero.
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
        if "context_pct" in result:
            break
    return result


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
        with open(path, encoding="utf-8-sig") as f:
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
    if source == "cursor" or payload.get("hook_event_name") in CURSOR_EVENTS:
        return build_cursor_event(payload, env)
    if payload.get("hook_event_name") == "Notification" and payload.get("notification_type") not in {
        "permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input",
    }:
        return None
    ev = {
        "session_id": payload.get("session_id") or "unknown",
        "event": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
        "agent": source,
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    hook_name = payload.get("hook_event_name")
    if payload.get("agent_id"):
        parent = payload.get("session_id") or "unknown"
        ev["parent_id"] = str(parent)[:128]
        ev["session_id"] = child_session_id(str(parent) + ":", payload["agent_id"])
        if hook_name in ("SubagentStart", "SubagentStop"):
            ev["event"] = "Stop" if hook_name == "SubagentStop" else "UserPromptSubmit"
        if payload.get("agent_type"):
            ev["name"] = str(payload["agent_type"]).strip()[:64]
    # Notification carries `message`; Codex's Stop carries `last_assistant_message`
    msg = (payload.get("message") or payload.get("last_assistant_message")) if wants_messages(env) else None
    if msg:
        ev["message"] = " ".join(str(msg).split())[:200]
    if source == "codex" and ev["event"] != "SessionEnd" and not ev.get("parent_id"):
        ev.update(codex_metrics(payload, env))
    ev.update(lifecycle_fields(payload))
    if ev.get("parent_id"):
        # Claude's child hooks carry the session-wide background list, including
        # the child itself and its siblings. It is not a list of this child's jobs.
        ev.pop("background_tasks", None)
        if hook_name == "SubagentStop":
            ev["background_tasks"] = []
    return ev


def lifecycle_fields(
    payload: dict[str, Any],
    ) -> dict[str, Any]:
    """
    Copy tool identities and reported background jobs without their commands or descriptions.

    Args:
        payload (dict[str, Any]): Provider lifecycle payload received over standard input.

    Returns:
        dict[str, Any]: Bounded identifiers and an authoritative job list when one was reported.
    """
    fields = {}
    for key in ("tool_use_id", "turn_id"):
        if isinstance(payload.get(key), str):
            fields[key] = _token(payload[key], 128)
    if isinstance(payload.get("tool_name"), str):
        fields["tool_name"] = _token(payload["tool_name"], 64)
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list):
        fields["background_tasks"] = [
            _token(task["id"], 128) for task in tasks[:64]
            if isinstance(task, dict) and isinstance(task.get("id"), str)
            and task.get("status") not in {"completed", "failed", "killed", "aborted"}
        ]
    return fields


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
    env: Mapping[str, str],
    ) -> tuple[str | None, str | None]:
    """
    Session and parent ids for a Cursor subagent, without its task text.

    Args:
        payload (dict[str, Any]): subagentStart or subagentStop input.
        env (Mapping[str, str]): Environment selecting local provider metadata.

    Returns:
        tuple[str | None, str | None]: (session_id, parent_id). Either is None when the payload has
            no identity.
    """
    try:
        from .runtime import cursor_child_identity
    except ImportError:
        from runtime import cursor_child_identity
    identity = cursor_child_identity(str(payload.get("conversation_id") or ""), env)
    if identity:
        parent, sub = identity
        return child_session_id("cursor:sub:", sub), ("cursor:" + parent)[:128]
    parent = payload.get("parent_conversation_id") or payload.get("conversation_id")
    sub = payload.get("subagent_id") or payload.get("tool_call_id")
    if not sub and payload.get("agent_transcript_path"):
        sub = Path(str(payload["agent_transcript_path"])).stem
    if not sub:
        return None, None
    parent_id = ("cursor:" + _token(parent, 80))[:128] if parent else None
    return child_session_id("cursor:sub:", sub), parent_id


def child_session_id(
    prefix: str,
    identity: Any,
    ) -> str:
    """
    Build a bounded child ID without dropping distinguishing suffixes of long identities.

    Args:
        prefix (str): Parent namespace or provider-specific child prefix.
        identity (Any): Provider-reported stable child or spawning tool identity.

    Returns:
        str: Original compact identity, or a deterministic SHA-256 identity within 128 characters.
    """
    value = prefix + _token(identity, 4096)
    return value if len(value) <= 128 else prefix[:50] + hashlib.sha256(value.encode()).hexdigest()


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


def display_title(
    *candidates: Any,
    ) -> str | None:
    """
    First non-empty public title/summary suitable for a tile label.

    Args:
        *candidates (Any): Preferred display strings, most specific first. Prompt bodies and
            transcript text must not be passed here.

    Returns:
        str | None: A single-line label capped at 64 characters, or None when nothing usable
            is present.
    """
    for value in candidates:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if text:
            return text[:64]
    return None


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
        dict[str, Any] | None: A piDeck lifecycle event with available local context and job IDs,
            or None for unrelated hooks and obsolete pre-compaction snapshots.
            Public summary/title fields may label the tile; prompts and task bodies do not.
    """
    hook_name = payload.get("hook_event_name")
    event = CURSOR_EVENTS.get(hook_name)
    sub_session, parent_id = cursor_subagent_ids(payload, env)
    if hook_name in ("subagentStart", "subagentStop") and not sub_session:
        return None  # Missing identity cannot safely start or finish a particular child.
    sid = payload.get("conversation_id") or payload.get("session_id") or (sub_session or "").removeprefix("cursor:")
    if event is None or not (sid or sub_session):
        return None
    roots = payload.get("workspace_roots") or []
    ev = {
        "session_id": sub_session or ("cursor:" + str(sid))[:128],
        "event": event,
        "agent": "cursor",
        "cwd": payload.get("cwd") or (roots[0] if roots else None),
    }
    if parent_id:
        ev["parent_id"] = parent_id
    if sub_session and parent_id != "cursor:" + str(sid):
        ev["provider_session_id"] = str(sid)[:128]
    if hook_name in ("subagentStart", "subagentStop") and payload.get("subagent_type"):
        ev["name"] = str(payload["subagent_type"]).strip()[:64]
    title = display_title(payload.get("summary"), payload.get("title"))
    if title:
        ev["name"] = title
    elif env.get("PIDECK_NAME") and "name" not in ev:
        ev["name"] = env["PIDECK_NAME"]
    model = payload.get("subagent_model")
    if isinstance(model, str) and model.strip():
        ev["detail"] = " ".join(model.split())[:64]
    else:
        detail = cursor_detail(payload)
        if detail:
            ev["detail"] = detail
    if event == "Stop":
        status = payload.get("status")
        if status == "error":
            ev["state"] = "error"
        elif status == "aborted":
            ev["event"] = "Interrupt"
    elif payload.get("hook_event_name") == "postToolUseFailure" and payload.get("failure_type") == "permission_denied":
        ev["state"] = "waiting"
    ev.update(lifecycle_fields(payload))
    if event != "SessionEnd" and (not sub_session or ev.get("provider_session_id")):
        try:
            from .runtime import cursor_metrics
        except ImportError:
            from runtime import cursor_metrics
        ev.update(cursor_metrics(str(sid), env))
    if hook_name == "stop" and ev.get("event") == "Stop" and "state" not in ev and "background_tasks" not in ev:
        ev["state"] = "unknown"  # A response end alone cannot confirm that background tools ended.
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
    if payload.get("turn-id"):
        ev["turn_id"] = str(payload["turn-id"])[:128]
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


# --- Event delivery and command-line entry point ---


def post(
    ev: dict[str, Any],
    env: Mapping[str, str],
    ) -> None:
    """
    Queue sanitized metadata and deliver it with the current hook-only credential.

    Args:
        ev (dict[str, Any]): Agent event containing the session identifier and permitted update
            fields.
        env (Mapping[str, str]): Environment variables used to locate integration settings and opt-
            in behavior.

    Returns:
        None: No value is returned.
    """
    conf = load_settings(env)
    ev = dict(ev)
    if ev.get("cwd") and env.get("PIDECK_SEND_PATHS") != "1":
        name = str(ev.pop("cwd")).replace("\\", "/").rstrip("/").split("/")[-1]
        ev.setdefault("name", name[:64])
    if not wants_messages(env):
        ev.pop("message", None)
    try:
        from .event_store import remember
    except ImportError:
        from event_store import remember
    ev = remember(ev, conf["url"], env)
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
        if "--statusline" in sys.argv[1:]:
            run_statusline(sys.stdin.read(), os.environ)
            return 0
        if "--engine" in sys.argv[1:]:
            payload = json.loads(sys.stdin.buffer.read())
            allowed = {"session_id", "event", "context_pct", "usage"}
            event = {key: value for key, value in payload.items() if key in allowed}
            if event.get("event") == "UsageUpdate" and event.get("session_id"):
                post({**event, "agent": "claude"}, os.environ)
            return 0
        source, notify = parse_args(sys.argv[1:])
        if notify is not None:
            ev = build_notify_event(json.loads(notify), os.environ)
        else:
            payload = json.loads(sys.stdin.buffer.read())
            if payload.get("hook_event_name") in CURSOR_EVENTS:
                source = "cursor"
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
