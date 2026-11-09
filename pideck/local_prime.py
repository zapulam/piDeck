"""

Publish local session and usage snapshots after piDeck is listening.

The hook script stays stdlib-only and is what actually reads the files. This module
only starts it, so a remote Pi never has to see the session transcripts.

"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


# --- Local hook discovery and startup refresh ---

def hook_script() -> Path | None:
    """
    Prefer the checkout's hook, then the copy the installer left behind.

    Returns:
        Path | None: The hook path to run, or None when neither copy exists.
    """
    repo = Path(__file__).resolve().parents[1] / "hooks" / "pideck_hook.py"
    installed = Path.home() / ".config" / "pideck" / "pideck_hook.py"
    if repo.is_file():
        return repo
    if installed.is_file():
        return installed
    return None


def launch_prime(
    url: str | None = None,
    force_claude_usage: bool = False,
    ) -> None:
    """
    Post already-open sessions and the latest allowance sample. Failures stay quiet.

    The child is killed if it is still running after 30 seconds, so a locked
    Cursor database cannot stall the display.

    Args:
        url (str | None): Owned local server address, preventing saved remote destination confusion.
        force_claude_usage (bool): When True, bypass Claude's 30-minute usage throttle once so a
            late piDeck launch still refreshes bars after Claude was already open.

    Returns:
        None: No value is returned.
    """
    script = hook_script()
    if script is None:
        return
    try:
        executable = Path(sys.executable)
        if getattr(sys, "frozen", False):
            executable = executable.with_name("piDeck-hook.exe" if sys.platform == "win32" else "piDeck-hook")
        arguments = [str(executable), str(script), "--prime"]
        env = dict(os.environ)
        if url:
            env["PIDECK_URL"] = url
        env.pop("PIDECK_TOKEN", None)
        if force_claude_usage:
            env["PIDECK_FORCE_CLAUDE_USAGE"] = "1"
        else:
            env.pop("PIDECK_FORCE_CLAUDE_USAGE", None)
        subprocess.run(arguments, timeout=30, check=False, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    except (OSError, subprocess.TimeoutExpired):
        return
