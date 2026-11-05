"""

Remember desktop window bounds and restore them safely on available displays.

"""

from __future__ import annotations

import json
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from hooks.pideck_hook import private_write


# --- Saved window placement ---


def windows_scale(
    native: Any,
    ) -> float:
    """
    Read actual per-window DPI because WinForms DeviceDpi can retain an unscaled value.

    Args:
        native (Any): Native WinForms window exposing its Windows handle.

    Returns:
        float: Logical-to-physical coordinate scale matching the pywebview renderer.
    """
    get_dpi = ctypes.WinDLL("user32").GetDpiForWindow
    get_dpi.argtypes = [wintypes.HWND]
    get_dpi.restype = wintypes.UINT
    return (get_dpi(native.Handle.ToInt64()) or 96) / 96


class WindowState:
    """Persist normal bounds separately from maximized or fullscreen presentation."""

    def __init__(
        self,
        path: Path,
        screens: list[Any],
        ) -> None:
        """
        Load validated placement and keep the restored window on an available monitor.

        Args:
            path (Path): Per-user JSON placement file, separate from account configuration.
            screens (list[Any]): Available pywebview screens in logical pixels.

        Returns:
            None: Validated creation options and normal bounds are initialized.
        """
        self.path = path
        self.window: Any = None
        self.bounds: dict[str, Any] = {"width": 960, "height": 640}
        self.maximized = False
        self.fullscreen = False
        self.desktop_height = screens[0].height if sys.platform == "darwin" and screens else None
        if self.desktop_height is not None:
            screens = [SimpleNamespace(x=screen.x, y=self.desktop_height - screen.y - screen.height,
                                       width=screen.width, height=screen.height) for screen in screens]
        try:
            if path.stat().st_size > 4096:
                raise ValueError("Oversized window state")
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("Invalid window state")
            for name, minimum, maximum in (("width", 360, 32768), ("height", 400, 32768),
                                            ("x", -100000, 100000), ("y", -100000, 100000)):
                value = saved.get(name)
                if type(value) is int and minimum <= value <= maximum:
                    self.bounds[name] = value
            self.maximized = saved.get("maximized") is True
            self.fullscreen = saved.get("fullscreen") is True
        except (OSError, ValueError):
            pass
        if screens:
            target = screens[0]
            x, y = self.bounds.get("x"), self.bounds.get("y")
            if x is not None and y is not None:
                target = max(screens, key=lambda screen: max(0, min(x + self.bounds["width"], screen.x + screen.width) - max(x, screen.x))
                             * max(0, min(y + self.bounds["height"], screen.y + screen.height) - max(y, screen.y)))
            self.bounds["width"] = max(360, min(self.bounds["width"], target.width))
            self.bounds["height"] = max(400, min(self.bounds["height"], target.height))
            if x is not None and y is not None:
                self.bounds["x"] = max(target.x, min(x, target.x + target.width - self.bounds["width"]))
                self.bounds["y"] = max(target.y, min(y, target.y + target.height - self.bounds["height"]))

    def options(
        self,
        ) -> dict[str, Any]:
        """
        Return the validated initial geometry accepted by pywebview.create_window.

        Returns:
            dict[str, Any]: Normal size/position and remembered maximized/fullscreen flags.
        """
        return {**self.bounds, "maximized": self.maximized, "fullscreen": self.fullscreen}

    def attach(
        self,
        window: Any,
        ) -> None:
        """
        Observe normal bounds and save placement synchronously before the native window closes.

        Args:
            window (Any): Native pywebview window whose geometry should survive a restart.

        Returns:
            None: Geometry and closing handlers are registered.
        """
        self.window = window
        window.events.resized += self.resized
        window.events.moved += self.moved
        window.events.closing += self.save
        if sys.platform == "darwin" and self.maximized:
            window.events.shown += self.restore_zoom

    def restore_zoom(
        self,
        ) -> None:
        """
        Restore Cocoa's native zoom state with its normal frame retained for unzooming.

        Returns:
            None: A zoom operation is queued on the macOS UI thread.
        """
        from PyObjCTools import AppHelper

        AppHelper.callAfter(self.window.native.zoom_, None)

    def presentation(
        self,
        ) -> tuple[bool, bool, bool]:
        """
        Read current native presentation without relying on asynchronous event ordering.

        Returns:
            tuple[bool, bool, bool]: Maximized, minimized, and fullscreen state respectively.
        """
        native = self.window.native
        if sys.platform == "win32":
            state = int(native.WindowState)
            return state == 2 or (state == 1 and self.maximized), state == 1, bool(self.window.fullscreen)
        if sys.platform == "darwin":
            return bool(native.isZoomed()), bool(native.isMiniaturized()), bool(native.styleMask() & (1 << 14))
        return False, False, bool(self.window.fullscreen)

    def resized(
        self,
        width: int,
        height: int,
        ) -> None:
        """
        Retain usable normal dimensions while ignoring minimized and expanded frames.

        Args:
            width (int): New logical window width.
            height (int): New logical window height.

        Returns:
            None: The latest normal size and presentation are retained in memory.
        """
        maximized, minimized, fullscreen = self.presentation()
        self.maximized, self.fullscreen = maximized, fullscreen
        if not (maximized or minimized or fullscreen) and width >= 360 and height >= 400:
            self.bounds.update(width=int(width), height=int(height))

    def moved(
        self,
        x: int,
        y: int,
        ) -> None:
        """
        Retain normal logical coordinates without recording minimized placeholder positions.

        Args:
            x (int): Window's logical horizontal coordinate.
            y (int): Window's logical vertical coordinate.

        Returns:
            None: The latest normal position is retained in memory.
        """
        if not any(self.presentation()):
            self.bounds.update(x=int(x), y=int(y))

    def save(
        self,
        ) -> None:
        """
        Save placement atomically without preventing closure if storage becomes unavailable.

        Returns:
            None: A private JSON file is updated or a safe warning is logged.
        """
        try:
            self.maximized, minimized, self.fullscreen = self.presentation()
            native = self.window.native
            if sys.platform == "win32" and not self.fullscreen:
                bounds = native.RestoreBounds if self.maximized or minimized else native.Bounds
                scale = windows_scale(native)
                self.bounds.update(x=round(bounds.X / scale), y=round(bounds.Y / scale),
                                   width=round(bounds.Width / scale), height=round(bounds.Height / scale))
            elif sys.platform == "darwin" and not (self.maximized or minimized or self.fullscreen):
                frame, screen = native.frame(), native.screen().frame()
                self.bounds.update(x=round(frame.origin.x),
                                   y=round((self.desktop_height or screen.size.height) - frame.origin.y - frame.size.height),
                                   width=round(frame.size.width), height=round(frame.size.height))
            private_write(self.path, json.dumps(self.options()) + "\n")
        except Exception:
            logging.getLogger(__name__).warning("Could not save desktop window placement")
