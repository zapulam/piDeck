"""Publish local session and usage snapshots after piDeck is listening.

The hook script stays stdlib-only and is what actually reads the files. This module
only starts it, so a remote Pi never has to see the session transcripts.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def hook_script() -> Path | None:
    """Prefer the checkout's hook, then the copy the installer left behind.

    Returns:
        The hook path to run, or None when neither copy exists.
    """
    repo = Path(__file__).resolve().parents[1] / "hooks" / "pideck_hook.py"
    installed = Path.home() / ".config" / "pideck" / "pideck_hook.py"
    if repo.is_file():
        return repo
    if installed.is_file():
        return installed
    return None


def launch_prime() -> None:
    """Post already-open sessions and the latest allowance sample. Failures stay quiet.

    The child is killed if it is still running after 30 seconds, so a locked
    Cursor database cannot stall the display.
    """
    script = hook_script()
    if script is None:
        return
    try:
        subprocess.run([sys.executable, str(script), "--prime"], timeout=30, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return
