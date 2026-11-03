"""Build and archive a native desktop package for the current operating system."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from scripts.release_metadata import ROOT, project_version


"""# --- Desktop packaging ---"""


def build(
        target: str,
) -> Path:
    """Freeze the app, embed build metadata, and create a platform-labelled ZIP archive.

    Args:
        target: Supported OS and architecture label matching the build machine.

    Returns:
        The generated desktop archive path.

    Raises:
        ValueError: The requested target does not match the build machine.
        subprocess.CalledProcessError: The native build or archive command fails.
    """
    machine = platform.machine().lower()
    actual = ("windows-" if sys.platform == "win32" else "macos-") + ("arm64" if machine in {"arm64", "aarch64"} else "x64")
    if sys.platform not in {"win32", "darwin"} or target != actual:
        raise ValueError(f"Target {target} cannot be built on {sys.platform}/{machine}")
    version = project_version()
    metadata = ROOT / "build" / "desktop-metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    info = {
        "version": version, "platform": target, "python": platform.python_version(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "dependencies": {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()},
    }
    (metadata / "build-info.json").write_text(json.dumps(info, indent=2, sort_keys=True), encoding="utf-8")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--name", "piDeck",
        "--add-data", "pideck/ui:pideck/ui", "--add-data", f"{metadata / 'build-info.json'}:.",
        "--add-data", "LICENSE:.", "--collect-all", "webview",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.websockets_impl", "--hidden-import", "uvicorn.lifespan.on",
    ]
    if sys.platform == "darwin":
        command.extend(["--osx-bundle-identifier", "com.zapulam.pideck"])
    subprocess.run([*command, "desktop_launcher.py"], cwd=ROOT, check=True)
    output = ROOT / "dist" / f"piDeck-{version}-{target}.zip"
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(ROOT / "dist/piDeck.app"), str(output)], check=True)
    else:
        shutil.make_archive(str(output.with_suffix("")), "zip", ROOT / "dist", "piDeck")
    return output


"""# --- Command entry point ---"""


def main() -> None:
    """Build the requested platform's desktop archive and print the output path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["windows-x64", "macos-arm64", "macos-x64"], required=True)
    print(build(parser.parse_args().platform))


if __name__ == "__main__":
    main()
