#!/usr/bin/env python3
"""Claude Code / Codex hook -> piDeck. Stdlib only, fast, and never allowed to break your session.

Two ways in, same script:

  Hooks (Claude Code and Codex CLI):  the tool pipes a JSON payload to stdin
      (session_id, cwd, hook_event_name, ...). Tell us which tool it is with --source:
          python3 pideck_hook.py                    # Claude Code (default)
          python3 pideck_hook.py --source codex     # Codex CLI

  Codex `notify` (fallback if you don't use hooks): Codex passes ONE JSON string as the last
  command-line argument instead of stdin and only reports "turn finished". Detected automatically.

Easiest setup: run hooks/install.py, which saves the address in ~/.config/pideck/hook.json
and registers this script. Environment variables, if set, override that file:
  PIDECK_URL             default http://localhost:8765   (use the Pi's address, e.g. http://pideck.local:8765)
  PIDECK_TOKEN           shared secret, if the server has one
  PIDECK_CONFIG          path to a different hook.json
  PIDECK_NAME            tile label override (default: the project folder name)
  PIDECK_CONTEXT_WINDOW  tokens for the ctx% estimate (default 200000; Claude Code only)
"""
import json
import os
import sys
import urllib.request

TAIL_BYTES = 256 * 1024


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


def build_event(payload, env, source="claude"):
    ev = {
        "session_id": payload.get("session_id") or "unknown",
        "event": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
        "agent": source,
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    # Notification carries `message`; Codex's Stop carries `last_assistant_message`
    msg = payload.get("message") or payload.get("last_assistant_message")
    if msg:
        ev["message"] = " ".join(str(msg).split())[:200]
    # ctx% is only computed for Claude Code: Codex transcripts have a different shape
    if source == "claude" and payload.get("transcript_path") and ev["event"] != "SessionEnd":
        pct = context_pct(payload["transcript_path"], float(env.get("PIDECK_CONTEXT_WINDOW", 200000)))
        if pct is not None:
            ev["context_pct"] = pct
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
    if payload.get("last-assistant-message"):
        ev["message"] = " ".join(str(payload["last-assistant-message"]).split())[:200]
    return ev


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
    path = env.get("PIDECK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "pideck", "hook.json")
    try:
        with open(path) as f:
            saved = json.load(f)
    except Exception:
        saved = {}
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
    try:
        source, notify = parse_args(sys.argv[1:])
        if notify is not None:
            ev = build_notify_event(json.loads(notify), os.environ)
        else:
            ev = build_event(json.load(sys.stdin), os.environ, source)
        if ev:
            post(ev, os.environ)
    except Exception:
        pass  # the display being offline must never affect the agent
    return 0  # and print nothing: Codex treats stray stdout from a Stop hook as a failure


if __name__ == "__main__":
    sys.exit(main())
