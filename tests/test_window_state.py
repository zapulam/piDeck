"""

Check desktop placement across resizing, maximization, invalid files, and monitor changes.

"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pideck import window_state
from pideck.window_state import WindowState


# --- Placement validation and native capture ---


@pytest.mark.parametrize("content", ["broken", "[]", '{"width":true,"height":-5,"x":"bad"}', " " * 4097])
def test_invalid_placement_uses_landscape_defaults(
    tmp_path: Path,
    content: str,
    ) -> None:
    """
    Ignore malformed or oversized saved placement without preventing startup.

    Args:
        tmp_path (Path): Temporary profile directory.
        content (str): Invalid saved JSON or geometry values.

    Returns:
        None: A safe landscape default is selected without invalid coordinates.
    """
    path = tmp_path / "window-state.json"
    path.write_text(content)
    assert WindowState(path, []).options() == {"width": 960, "height": 640, "maximized": False, "fullscreen": False}


def test_monitor_changes_keep_placement_visible(
    tmp_path: Path,
    ) -> None:
    """
    Preserve a secondary-monitor position and recover it after that monitor is removed.

    Args:
        tmp_path (Path): Temporary profile directory.

    Returns:
        None: Saved geometry remains visible and fits the selected monitor.
    """
    path = tmp_path / "window-state.json"
    path.write_text(json.dumps({"width": 1200, "height": 800, "x": -1500, "y": 100}))
    primary = SimpleNamespace(x=0, y=0, width=1280, height=720)
    secondary = SimpleNamespace(x=-1920, y=0, width=1920, height=1080)
    assert WindowState(path, [primary, secondary]).bounds == {"width": 1200, "height": 800, "x": -1500, "y": 100}
    assert WindowState(path, [primary]).bounds == {"width": 1200, "height": 720, "x": 0, "y": 0}


@pytest.mark.parametrize("state", [0, 1, 2])
def test_windows_close_restores_logical_normal_bounds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: int,
    ) -> None:
    """
    Capture Windows restore bounds in logical pixels without saving minimized dimensions.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper restoring the simulated platform.
        tmp_path (Path): Temporary profile directory.
        state (int): Normal, minimized, or maximized native window state.

    Returns:
        None: Reopening retains normal geometry and the pre-minimize maximized state.
    """
    monkeypatch.setattr(window_state.sys, "platform", "win32")
    monkeypatch.setattr(window_state, "windows_scale", lambda native: 1.5)
    path = tmp_path / "window-state.json"
    placement = WindowState(path, [])
    placement.maximized = state != 0
    bounds = SimpleNamespace(X=90, Y=120, Width=1500, Height=900)
    placement.window = SimpleNamespace(fullscreen=False, native=SimpleNamespace(
        WindowState=state, DeviceDpi=144, Bounds=bounds, RestoreBounds=bounds))
    placement.resized(1920 if state == 2 else 0, 1080 if state == 2 else 0)
    placement.save()
    restored = WindowState(path, []).options()
    assert restored == {"x": 60, "y": 80, "width": 1000, "height": 600,
                        "maximized": state != 0, "fullscreen": False}


def test_macos_close_captures_frame_without_gui_thread_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ) -> None:
    """
    Read the native Cocoa frame directly during closing and convert its vertical coordinate.

    Args:
        monkeypatch (pytest.MonkeyPatch): Helper restoring the simulated platform.
        tmp_path (Path): Temporary profile directory.

    Returns:
        None: The saved native frame uses pywebview-compatible top-left coordinates.
    """
    monkeypatch.setattr(window_state.sys, "platform", "darwin")
    monitors = [SimpleNamespace(x=0, y=0, width=1920, height=1080),
                SimpleNamespace(x=0, y=1080, width=1600, height=900)]
    placement = WindowState(tmp_path / "window-state.json", monitors)
    frame = SimpleNamespace(origin=SimpleNamespace(x=50, y=1200), size=SimpleNamespace(width=900, height=600))
    screen = SimpleNamespace(frame=lambda: SimpleNamespace(size=SimpleNamespace(height=900)))
    placement.window = SimpleNamespace(native=SimpleNamespace(isZoomed=lambda: False,
        isMiniaturized=lambda: False, styleMask=lambda: 0, frame=lambda: frame, screen=lambda: screen))
    placement.save()
    restored = WindowState(placement.path, monitors)
    assert restored.bounds == {"x": 50, "y": -720, "width": 900, "height": 600}
