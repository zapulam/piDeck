"""Config loading: config.yaml merged over defaults; secrets come from the environment (.env)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8765},
    "views": ["agents", "now_playing", "slack"],
    "mock": False,
    "ui": {"rotate": 0},
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


PAGES = ("agents", "now_playing", "slack")


"""# --- Saved page selection ---"""


def ui_settings_path(
        config_path: Path,
) -> Path:
    """Return the gitignored page-selection file that belongs with a config.

    Args:
        config_path: The config.yaml this server was started with.

    Returns:
        ui-settings.json beside that config, or in the working directory when
        the config directory does not exist.
    """
    parent = Path(config_path).expanduser().resolve().parent
    if not parent.is_dir():
        parent = Path.cwd()
    return parent / "ui-settings.json"


def load_saved_views(
        config_path: Path,
        default: list[str],
) -> list[str]:
    """Return the pages to show, preferring the gitignored selection file.

    Args:
        config_path: The config.yaml this server was started with.
        default: Page ids from config.yaml, used when no selection has been saved.

    Returns:
        Known page ids in display order. Settings is not included; the UI always adds it.
    """
    fallback = [page for page in PAGES if page in default]
    path = ui_settings_path(config_path)
    if not path.is_file():
        return fallback
    try:
        chosen = json.loads(path.read_text(encoding="utf-8")).get("views")
    except (OSError, ValueError):
        return fallback
    if not isinstance(chosen, list):
        return fallback
    return [page for page in PAGES if page in chosen]


def save_views(
        config_path: Path,
        views: list[str],
) -> list[str]:
    """Write the selected pages to the gitignored selection file.

    Args:
        config_path: The config.yaml this server was started with.
        views: Requested page ids. Unknown ids are dropped and order is normalized.

    Returns:
        The page ids that were saved.
    """
    chosen = [page for page in PAGES if page in views]
    path = ui_settings_path(config_path)
    path.write_text(json.dumps({"views": chosen}, indent=2) + "\n", encoding="utf-8")
    return chosen


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    p = Path(path) if path else Path("config.yaml")
    load_dotenv(p.parent / ".env")
    data: dict = {}
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
    return _merge(DEFAULTS, data)
