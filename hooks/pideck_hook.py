#!/usr/bin/env python3
"""Claude Code hook -> piDeck. Stdlib only, fast, and never allowed to break your session.

Claude Code pipes a JSON payload to stdin (session_id, cwd, hook_event_name, transcript_path,
and `message` for Notification events). We forward a tiny event to the piDeck server.

Env:
  PIDECK_URL             default http://localhost:8765   (use the Pi's address, e.g. http://pideck.local:8765)
  PIDECK_TOKEN           shared secret, if the server has one
  PIDECK_NAME            tile label override (default: the project folder name)
  PIDECK_CONTEXT_WINDOW  tokens for the ctx% estimate (default 200000; set 1000000 for 1M-context models)
"""
import json
import os
import sys
import urllib.request

TAIL_BYTES = 256 * 1024


def context_pct(transcript_path, window):
    """Best-effort: tokens in the latest assistant turn's prompt / window. None if unknown."""
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


def build_event(payload, env):
    ev = {
        "session_id": payload.get("session_id") or "unknown",
        "event": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
    }
    if env.get("PIDECK_NAME"):
        ev["name"] = env["PIDECK_NAME"]
    if payload.get("message"):
        ev["message"] = str(payload["message"])[:200]
    if payload.get("transcript_path") and ev["event"] != "SessionEnd":
        pct = context_pct(payload["transcript_path"], float(env.get("PIDECK_CONTEXT_WINDOW", 200000)))
        if pct is not None:
            ev["context_pct"] = pct
    return ev


def main():
    try:
        payload = json.load(sys.stdin)
        ev = build_event(payload, os.environ)
        headers = {"Content-Type": "application/json"}
        if os.environ.get("PIDECK_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["PIDECK_TOKEN"]
        url = os.environ.get("PIDECK_URL", "http://localhost:8765").rstrip("/") + "/api/agents/event"
        urllib.request.urlopen(
            urllib.request.Request(url, json.dumps(ev).encode(), headers), timeout=2
        ).read()
    except Exception:
        pass  # the display being offline must never affect Claude Code
    return 0


if __name__ == "__main__":
    sys.exit(main())
