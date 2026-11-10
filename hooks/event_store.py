"""

Keep sanitized provider events until a local display can consume them.

SQLite transactions serialize concurrent hooks; an event keeps the same identity
whether it arrives over HTTP or through the desktop's offline replay.

"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Mapping
import uuid
from urllib.parse import urlsplit


# --- Durable event delivery ---


def store_path(
    env: Mapping[str, str],
    ) -> Path:
    """
    Locate the per-user event spool next to the hook configuration.

    Args:
        env (Mapping[str, str]): Environment containing an optional PIDECK_CONFIG override.

    Returns:
        Path: Database containing display metadata, never provider credentials or transcripts.
    """
    config = Path(env.get("PIDECK_CONFIG") or Path.home() / ".config/pideck/hook.json")
    return config.with_name("events.sqlite3")


def destination(
    url: str,
    ) -> str:
    """
    Normalize loopback aliases without merging different display destinations.

    Args:
        url (str): Configured HTTP or HTTPS display address.

    Returns:
        str: Canonical destination including transport, host, port, and path.
    """
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host in {"127.0.0.1", "localhost", "::1"}:
        host = "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{host}:{port}{parsed.path.rstrip('/')}"


def remember(
    event: dict[str, Any],
    url: str,
    env: Mapping[str, str],
    ) -> dict[str, Any]:
    """
    Save one sanitized hook event before attempting network delivery.

    Args:
        event (dict[str, Any]): Whitelisted display fields produced by the provider adapter.
        url (str): Intended display destination, keeping remote and local events separate.
        env (Mapping[str, str]): Environment selecting the hook configuration directory.

    Returns:
        dict[str, Any]: Event with a stable identity and provider observation time.
    """
    result = {**event, "event_id": uuid.uuid4().hex, "activity_at": time.time()}
    path = store_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            descriptor, staging = tempfile.mkstemp(prefix=".events-", dir=path.parent)
            os.close(descriptor)
            try:
                if os.name == "nt":
                    try:
                        from .pideck_hook import protect_file
                    except ImportError:
                        from pideck_hook import protect_file
                    protect_file(Path(staging))
                # Publish an already-private empty file without replacing another
                # hook's concurrently created database or spawning per-event ACL tools.
                try:
                    os.link(staging, path)
                except FileExistsError:
                    pass
            finally:
                Path(staging).unlink(missing_ok=True)
        if os.name != "nt":
            path.chmod(0o600)
        with sqlite3.connect(path, timeout=2) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, target TEXT, body TEXT)")
            connection.execute("INSERT INTO events(target, body) VALUES (?, ?)",
                               (destination(url), json.dumps(result)))
            connection.execute("DELETE FROM events WHERE seq < (SELECT COALESCE(MAX(seq), 0) - 20000 FROM events)")
    except (OSError, sqlite3.Error, subprocess.SubprocessError):
        pass  # Network delivery can still succeed when local storage is unavailable.
    return result


def pending(
    url: str,
    after: int,
    env: Mapping[str, str],
    ) -> list[tuple[int, dict[str, Any]]]:
    """
    Read queued events without creating a database or changing provider state.

    Args:
        url (str): Owned display address whose events should be consumed.
        after (int): Last consumed sequence number.
        env (Mapping[str, str]): Environment selecting the hook configuration directory.

    Returns:
        list[tuple[int, dict[str, Any]]]: Ordered sequence numbers and sanitized event objects.
    """
    path = store_path(env)
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            rows = connection.execute("SELECT seq, body FROM events WHERE target = ? AND seq > ? ORDER BY seq LIMIT 2000",
                                      (destination(url), after)).fetchall()
        finally:
            connection.close()
        return [(seq, json.loads(body)) for seq, body in rows]
    except (OSError, ValueError, sqlite3.Error):
        return []
