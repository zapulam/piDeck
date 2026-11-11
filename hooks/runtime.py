"""

Read bounded local provider metadata without opening credentials or modifying provider databases.

"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping


# --- Cursor context and background terminals ---


def cursor_child_identity(
    session_id: str,
    env: Mapping[str, str],
    ) -> tuple[str, str] | None:
    """
    Read the provider-written parent and spawning tool ID for one Cursor child conversation.

    Args:
        session_id (str): Conversation ID reported by the current hook.
        env (Mapping[str, str]): Environment selecting Cursor's local state database.

    Returns:
        tuple[str, str] | None: Parent conversation and spawning tool IDs for the verified
            subagentInfo schema, or None when identity cannot be established.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return None
    try:
        path = cursor_database(env)
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            row = connection.execute("SELECT value FROM cursorDiskKV WHERE key = ? AND length(value) <= 524288",
                                     ("composerData:" + session_id,)).fetchone()
        finally:
            connection.close()
        composer = json.loads(row[0]) if row else None
        info = composer.get("subagentInfo") if isinstance(composer, dict) else None
        if not isinstance(info, dict):
            return None
        parent, tool = info.get("parentComposerId"), info.get("toolCallId")
        if isinstance(parent, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parent) and parent != session_id and isinstance(tool, str) and tool.strip():
            return parent, tool
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return None


def cursor_database(
    env: Mapping[str, str],
    ) -> Path:
    """
    Locate Cursor's local state database for this operating system.

    Args:
        env (Mapping[str, str]): Environment supporting a CURSOR_STATE_DB test override.

    Returns:
        Path: Provider-owned SQLite database to open in read-only mode.
    """
    if env.get("CURSOR_STATE_DB"):
        return Path(env["CURSOR_STATE_DB"])
    home = Path(env.get("PIDECK_HOME") or Path.home())
    if os.name == "nt":
        base = Path(env.get("APPDATA") or home / "AppData/Roaming")
    elif os.sys.platform == "darwin":
        base = home / "Library/Application Support"
    else:
        base = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    return base / "Cursor/User/globalStorage/state.vscdb"


def terminal_running(
    home: Path,
    shell_id: str,
    ) -> bool:
    """
    Check a reported Cursor background terminal's structured completion footer.

    Args:
        home (Path): Provider home directory containing .cursor/projects.
        shell_id (str): Numeric terminal identifier reported by Cursor itself.

    Returns:
        bool: False only for a terminal with a complete provider-written exit footer.
    """
    if not re.fullmatch(r"\d{1,20}", shell_id):
        return True
    found = False
    for path in (home / ".cursor/projects").glob(f"*/terminals/{shell_id}.txt"):
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 1024))
                tail = stream.read().decode("utf-8", "ignore")
        except OSError:
            return True
        found = True
        if not re.search(r"\n---\r?\nexit_code: -?\d+\r?\nelapsed_ms: \d+\r?\nended_at: [^\r\n]+\r?\n---\s*\Z", tail):
            return True
    return not found  # Missing or ambiguous metadata must never announce completion.


def cursor_metrics(
    session_id: str,
    env: Mapping[str, str],
    ) -> dict[str, Any]:
    """
    Read actual context and outstanding terminal IDs for one known Cursor conversation.

    Args:
        session_id (str): Exact conversation identifier received from a live hook.
        env (Mapping[str, str]): Environment selecting the provider home and state database.

    Returns:
        dict[str, Any]: Available context percentage and a known-schema background job list.
            Unknown schemas or incomplete reads omit the job list instead of clearing it.
    """
    path = cursor_database(env)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        return {}
    if not path.is_file():
        return {}
    result = {}
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            deadline = time.monotonic() + 0.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            row = connection.execute("SELECT value FROM cursorDiskKV WHERE key = ?",
                                     ("composerData:" + session_id,)).fetchone()
            if row is None:
                return {}
            composer = json.loads(row[0])
            if not isinstance(composer, dict) or composer.get("status") not in {"none", "generating", "completed", "aborted"}:
                return {}
            pct = composer.get("contextUsagePercent")
            if isinstance(pct, (int, float)) and 0 <= pct <= 100:
                result["context_pct"] = pct
            # Only inspect one conversation. Large or unfamiliar records invalidate
            # the completeness claim, but do not prevent useful context reporting.
            prefix = "bubbleId:" + session_id + ":"
            # A binary key range uses Cursor's primary-key index. LIKE defaults to
            # case-insensitive comparison and scanned the full live database.
            sizes = connection.execute(
                "SELECT key, length(value) FROM cursorDiskKV WHERE key >= ? AND key < ? LIMIT 2001",
                (prefix, prefix + "\uffff"),
            ).fetchall()
            if len(sizes) > 2000 or sum(length for _, length in sizes) > 2_000_000:
                return result
            rows = [connection.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,)).fetchone()
                    for key, _ in sizes]
        finally:
            connection.close()
        jobs = set()
        for (raw,) in rows:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return result
            tool = data.get("toolFormerData")
            if not isinstance(tool, dict):
                continue
            value = tool.get("result")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    return result
            if isinstance(value, dict) and value.get("backgroundShellId"):
                shell = str(value["backgroundShellId"])
                if terminal_running(Path(env.get("PIDECK_HOME") or Path.home()), shell):
                    jobs.add(shell)
        result["background_tasks"] = sorted(jobs)[:64]
    except (OSError, ValueError, TypeError, sqlite3.Error):
        pass
    return result


# --- Codex read receipts ---


def codex_viewed(
    session_id: str,
    turn_id: str,
    home: Path,
    ) -> bool:
    """
    Recognize the exact completed turn acknowledged by the installed Codex receipt schema.

    Args:
        session_id (str): Codex thread whose completed tile may be removed.
        turn_id (str): Provider-reported completed turn, preventing an old receipt from matching.
        home (Path): Provider home containing .codex/state_5.sqlite.

    Returns:
        bool: True only for the verified schema and a matching published turn with no unread turn.
    """
    if not turn_id:
        return False
    path = home / ".codex/state_5.sqlite"
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(thread_read_receipts)")}
            if columns != {"thread_id", "first_unread_turn", "revision", "last_published_turn"}:
                return False
            row = connection.execute(
                "SELECT first_unread_turn, last_published_turn FROM thread_read_receipts WHERE thread_id = ?",
                (session_id,),
            ).fetchone()
            return row is not None and row[0] is None and row[1] == turn_id
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False
