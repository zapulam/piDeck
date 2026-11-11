"""

Read Codex account allowances through its documented app-server protocol.

This owns a separate, short-lived connection for account reads only. It never
resumes conversations or treats that server's thread status as Desktop status.

"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping
from hooks.pideck_hook import usage_window
from . import __version__


# --- Codex executable discovery ---


def codex_executable(
    env: Mapping[str, str],
    ) -> str | None:
    """
    Find the installed Codex CLI or Desktop-bundled app-server executable.

    Args:
        env (Mapping[str, str]): Environment with optional CODEX_EXECUTABLE and platform paths.

    Returns:
        str | None: Existing executable path, or None when Codex is not installed.
    """
    override = env.get("CODEX_EXECUTABLE")
    if override:
        return override if Path(override).is_file() else None
    command = shutil.which("codex")
    if command and (os.name != "nt" or Path(command).suffix.lower() == ".exe"):
        return command
    candidates = []
    if env.get("LOCALAPPDATA"):
        candidates.extend((Path(env["LOCALAPPDATA"]) / "OpenAI/Codex/bin").glob("*/codex.exe"))
    candidates.extend(Path("/Applications/Codex.app/Contents/Resources").glob("codex"))
    return str(max(candidates, key=lambda path: path.stat().st_mtime)) if candidates else None


# --- Bounded account-only RPC ---


async def rpc(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
    """
    Exchange one JSON-lines request without exposing authentication or provider output.

    Args:
        process (asyncio.subprocess.Process): Child app-server owned by this reader.
        request_id (int): Connection-local correlation identifier.
        method (str): Documented read-only method or initialization handshake.
        params (dict[str, Any] | None): Optional public protocol parameters.

    Returns:
        dict[str, Any]: Matching RPC result; errors raise ValueError and become unavailable usage.
    """
    request = {"id": request_id, "method": method}
    if params is not None:
        request["params"] = params
    process.stdin.write((json.dumps(request) + "\n").encode())
    await process.stdin.drain()
    deadline = asyncio.get_running_loop().time() + 8
    for _ in range(40):
        raw = await asyncio.wait_for(process.stdout.readline(), max(0, deadline - asyncio.get_running_loop().time()))
        if not raw or len(raw) > 65536:
            raise ValueError("Invalid account response")
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            raise ValueError("Invalid account response")
        if reply.get("id") == request_id:
            if "error" in reply:
                raise ValueError("Account usage unavailable")
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ValueError("Invalid account response")
            return result
    raise ValueError("Missing account response")


async def allowances(
    env: Mapping[str, str],
    ) -> dict[str, Any] | None:
    """
    Read current allowance windows without reading or refreshing account credentials ourselves.

    Args:
        env (Mapping[str, str]): Environment used to locate the installed Codex executable.

    Returns:
        dict[str, Any] | None: piDeck usage windows, or None for unsupported or unavailable accounts.
    """
    executable = codex_executable(env)
    if executable is None:
        return None
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "app-server", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
        await rpc(process, 1, "initialize", {"clientInfo": {"name": "pideck", "version": __version__}})
        process.stdin.write(b'{"method":"initialized"}\n')
        await process.stdin.drain()
        result = await rpc(process, 2, "account/rateLimits/read")
        groups = result.get("rateLimitsByLimitId")
        limits = (groups.get("codex") if isinstance(groups, dict) else None) or result.get("rateLimits") or {}
        if not isinstance(limits, dict):
            return None
        windows = {}
        for name in ("primary", "secondary"):
            value = limits.get(name)
            windows[name] = usage_window(value, "usedPercent", value.get("windowDurationMins")) if isinstance(value, dict) else None
        return windows if any(windows.values()) else None
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    finally:
        if process is not None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
