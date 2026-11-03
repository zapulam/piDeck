"""Exercise the frozen application's backend and bundled assets without a display server."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.release_metadata import project_version


"""# --- Packaged application validation ---"""


def smoke_test(
        directory: Path,
) -> dict:
    """Launch a frozen app's non-GUI self-test and verify its machine-readable result.

    Args:
        directory: PyInstaller output directory holding the platform's app.

    Returns:
        The successful self-test report.

    Raises:
        RuntimeError: The frozen executable fails or reports incomplete validation.
        subprocess.TimeoutExpired: Startup and validation take longer than sixty seconds.
    """
    executable = directory / ("piDeck/piDeck.exe" if sys.platform == "win32" else "piDeck.app/Contents/MacOS/piDeck")
    with tempfile.TemporaryDirectory(prefix="pideck-package-test-") as temp:
        report = Path(temp) / "report.json"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        result = subprocess.run([str(executable.resolve()), "--self-test", "--report", str(report)],
                                timeout=60, capture_output=True, **options)
        if result.returncode or not report.exists():
            raise RuntimeError(f"Packaged self-test failed ({result.returncode}): {result.stderr[-2000:]!r}")
        data = json.loads(report.read_text(encoding="utf-8"))
        if not data.get("ok") or data.get("version") != project_version():
            raise RuntimeError(f"Packaged self-test returned an invalid report: {data}")
        return data


"""# --- Command entry point ---"""


def main() -> None:
    """Validate the desktop executable in a supplied distribution directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable-dir", type=Path, required=True)
    print(json.dumps(smoke_test(parser.parse_args().executable_dir), indent=2))


if __name__ == "__main__":
    main()
