"""

Exercise the account-only Codex JSON-lines protocol through real isolated subprocess pipes.

"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from pideck import codex_usage


# --- Account protocol and schema boundaries ---


@pytest.mark.parametrize("reply", [
    {"rateLimits": {"primary": {"usedPercent": 27, "windowDurationMins": 300, "resetsAt": 2000000000}}},
    {"rateLimitsByLimitId": {"codex": {"secondary": {"usedPercent": 91, "windowDurationMins": 10080}}}},
    {"rateLimits": []},
    {"rateLimits": {"primary": {"usedPercent": 101, "windowDurationMins": 300}}},
])
def test_only_account_reads_and_owned_process_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
    ) -> None:
    """
    Read valid allowances and reject unknown shapes without touching conversation runtime.

    Args:
        tmp_path (Path): Directory containing the isolated protocol peer and request log.
        monkeypatch (pytest.MonkeyPatch): Redirect the owned app-server launch to a fake peer.
        reply (dict[str, Any]): Provider account result to validate through the real pipe transport.

    Returns:
        None: Only initialization and account reads occur, and EOF closes the owned subprocess.
    """
    peer = tmp_path / "peer.py"
    recorded = tmp_path / "requests.jsonl"
    peer.write_text(
        "import sys,json\n"
        f"result={reply!r}\n"
        f"with open({str(recorded)!r}, 'w', encoding='utf-8') as log:\n"
        " for line in sys.stdin:\n"
        "  req=json.loads(line); log.write(line); log.flush()\n"
        "  if 'id' in req:\n"
        "   print(json.dumps({'id':req['id'],'result':result if req['method']=='account/rateLimits/read' else {}}),flush=True)\n",
        encoding="utf-8",
    )
    original = asyncio.create_subprocess_exec
    children = []
    async def launch(
        *args: str,
        **kwargs: Any,
        ) -> asyncio.subprocess.Process:
        """
        Substitute a protocol peer while retaining actual asynchronous standard streams.

        Args:
            args (str): Original executable and app-server arguments, checked for account-only launch.
            kwargs (Any): Subprocess stream and platform creation options.

        Returns:
            asyncio.subprocess.Process: Owned isolated child exercising the production transport.
        """
        assert args[1:] == ("app-server",)
        child = await original(sys.executable, str(peer), **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = asyncio.run(codex_usage.allowances({"CODEX_EXECUTABLE": sys.executable}))
    methods = [json.loads(line)["method"] for line in recorded.read_text(encoding="utf-8").splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]
    assert children[0].returncode == 0
    if reply.get("rateLimitsByLimitId"):
        assert result["secondary"]["used_pct"] == 91
    elif isinstance(reply.get("rateLimits"), dict) and reply["rateLimits"]["primary"]["usedPercent"] == 27:
        assert result["primary"]["used_pct"] == 27
    else:
        assert result is None
