"""Config loading: config.yaml merged over defaults; secrets come from the environment (.env)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8765},
    "views": ["agents", "now_playing", "slack"],
    "mock": False,
    "alerts": {"seconds": 8},
    "modules": {
        "agents": {
            "enabled": True,
            "alert_on": ["done", "waiting"],
            "stale_hours": 12,
            "context_window": 200000,
        },
        "spotify": {"enabled": True, "poll_seconds": 3},
        "slack": {
            "enabled": True,
            "alert_on": ["dm", "mention"],
            "channels": [],
            "keep": 30,
        },
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    p = Path(path) if path else Path("config.yaml")
    load_dotenv(p.parent / ".env")
    data: dict = {}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    return _merge(DEFAULTS, data)
