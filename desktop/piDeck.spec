"""

Build a native desktop app and a console-capable hook runner sharing one bundled runtime.

"""

from pathlib import Path
import os
import re
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

# --- Platform bundle inputs ---

root = Path(SPECPATH).parent
version = re.search(r'__version__ = "([^"]+)"', (root / "pideck/__init__.py").read_text()).group(1).split("-")[0]
windows = sys.platform == "win32"
mac = sys.platform == "darwin"
icon = str(root / "pideck/assets" / ("pideck.ico" if windows else "pideck.icns"))
hidden = ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
          "uvicorn.protocols.websockets.websockets_impl", "uvicorn.protocols.websockets.websockets_sansio_impl",
          "uvicorn.lifespan.on", "websockets", "hooks.install", "certifi"]
hidden += ["webview.platforms.winforms", "webview.platforms.edgechromium", "keyring.backends.Windows"] if windows else ["webview.platforms.cocoa", "keyring.backends.macOS"]
data = [(str(root / "pideck/ui"), "pideck/ui"), (str(root / "pideck/assets"), "pideck/assets"),
        (str(root / ".env.example"), "."), (str(root / "hooks/pideck_hook.py"), "hooks"),
        (str(root / "hooks/event_store.py"), "hooks"), (str(root / "hooks/runtime.py"), "hooks"),
        (str(root / "hooks/claude_engine.ts"), "hooks"),
        (str(root / "LICENSE"), ".")]
data += collect_data_files("webview") + collect_data_files("certifi") + copy_metadata("pywebview")

# --- Shared native application and hook runtime ---

app = Analysis([str(root / "desktop/entry.py")], pathex=[str(root)],
               binaries=collect_dynamic_libs("webview"), datas=data, hiddenimports=hidden,
               excludes=["pytest", "playwright", "PIL", "tkinter", "PyQt5", "PyQt6", "PySide6"], noarchive=False)
hook = Analysis([str(root / "desktop/hook_entry.py")], pathex=[str(root)],
                datas=collect_data_files("certifi"), excludes=["tkinter"], noarchive=False)
identity = os.environ.get("MACOS_SIGNING_IDENTITY") or None
app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="piDeck", console=False,
              icon=icon, codesign_identity=identity, entitlements_file=str(root / "desktop/entitlements.plist") if mac else None)
hook_exe = EXE(PYZ(hook.pure), hook.scripts, [], exclude_binaries=True, name="piDeck-hook", console=True,
               icon=icon if windows else None, codesign_identity=identity,
               entitlements_file=str(root / "desktop/entitlements.plist") if mac else None)
bundle = COLLECT(app_exe, hook_exe, app.binaries, app.datas, hook.binaries, hook.datas, name="piDeck")
if mac:
    bundle = BUNDLE(bundle, name="piDeck.app", icon=icon, bundle_identifier="com.zapulam.pideck",
                    codesign_identity=identity, info_plist={"CFBundleShortVersionString": version,
                    "CFBundleVersion": version, "NSHighResolutionCapable": True})
