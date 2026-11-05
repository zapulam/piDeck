"""

Audit locked dependencies, bundle native applications, and create verifiable desktop installers.

"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pideck import __version__


# --- Verified compiler and Windows runtime prerequisites ---

INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe"
INNO_SHA256 = "8a5a6711c3766a6e5fb74f280ba0e3551e719ce94538d901fdbd1f8f9920b3f0"


def run(
    arguments: list[str],
    ) -> None:
    """
    Run a build tool without shell interpretation and fail on an unsuccessful exit.

    Args:
        arguments (list[str]): Executable and individual arguments, excluding logged secrets.

    Returns:
        None: The build step completed successfully.
    """
    subprocess.run(arguments, cwd=ROOT, check=True)


def download(
    url: str,
    path: Path,
    ) -> None:
    """
    Download a public build prerequisite for signature or pinned-hash verification.

    Args:
        url (str): Public HTTPS download address carrying no credentials.
        path (Path): Destination inside the disposable build directory.

    Returns:
        None: Downloaded bytes are persisted for verification before execution.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def verify_publisher(
    path: Path,
    publisher: str,
    ) -> None:
    """
    Verify a Windows prerequisite's Authenticode signature and expected organization.

    Args:
        path (Path): Downloaded executable, which has not yet been run.
        publisher (str): Required signer organization substring.

    Returns:
        None: Signature and publisher were validated or the build fails.
    """
    env = {**os.environ, "PIDECK_VERIFY_PATH": str(path.resolve()), "PIDECK_VERIFY_PUBLISHER": publisher}
    env.pop("PSModulePath", None)
    shell = shutil.which("pwsh.exe") or "powershell.exe"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
                    "Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop; "
                    "$s=Get-AuthenticodeSignature -LiteralPath $env:PIDECK_VERIFY_PATH; "
                    "Write-Output ('Prerequisite signature: ' + $s.Status + '; ' + $s.StatusMessage + '; ' + $s.SignerCertificate.Subject); "
                    "if($s.Status -ne 'Valid' -or -not $s.SignerCertificate.Subject.Contains($env:PIDECK_VERIFY_PUBLISHER)){exit 1}"],
                            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            creationflags=0x08000000)
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.returncode:
        raise RuntimeError("Prerequisite signature verification failed: " + result.stderr.strip())


def windows_compiler() -> Path:
    """
    Install a hash-pinned, signature-verified portable Inno compiler beneath build tools.

    Returns:
        Path: Verified compiler executable without a machine-wide installation.
    """
    directory = ROOT / "build/tools/inno7"
    compiler = directory / "ISCC.exe"
    if compiler.is_file():
        verify_publisher(compiler, "O=Pyrsys B.V.")
        return compiler
    setup = ROOT / "build/tools/innosetup-7.1.1-x64.exe"
    download(INNO_URL, setup)
    if hashlib.sha256(setup.read_bytes()).hexdigest() != INNO_SHA256:
        raise RuntimeError("Inno Setup download checksum mismatch")
    verify_publisher(setup, "O=Pyrsys B.V.")
    run([str(setup), "/CURRENTUSER", "/PORTABLE=1", "/VERYSILENT", "/SUPPRESSMSGBOXES",
         "/NORESTART", "/NOICONS", f"/DIR={directory}"])
    return compiler


# --- Native bundle and installer construction ---


def mac_icon() -> None:
    """
    Render the bundled raspberry PNG into the native macOS icon representation.

    Returns:
        None: A generated ICNS file is available for PyInstaller.
    """
    iconset = ROOT / "build/pideck.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    source = ROOT / "pideck/assets/pideck.png"
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            edge = size * scale
            suffix = "@2x" if scale == 2 else ""
            run(["sips", "-z", str(edge), str(edge), str(source), "--out", str(iconset / f"icon_{size}x{size}{suffix}.png")])
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "pideck/assets/pideck.icns")])


def sign_windows(
    paths: list[Path],
    ) -> bool:
    """
    Optionally sign application executables or installers using an externally supplied certificate.

    Args:
        paths (list[Path]): Windows executables to sign before packaging or publication.

    Returns:
        bool: True after signing, or False when no certificate was supplied.
    """
    certificate = os.environ.get("WINDOWS_SIGN_CERT_PATH")
    if not certificate:
        return False
    candidates = sorted(Path("C:/Program Files (x86)/Windows Kits/10/bin").glob("*/x64/signtool.exe"))
    tool = shutil.which("signtool.exe") or (str(candidates[-1]) if candidates else None)
    if not tool:
        raise RuntimeError("Windows SDK signtool is required for signed builds")
    for path in paths:
        try:
            subprocess.run([tool, "sign", "/fd", "SHA256", "/f", certificate, "/p",
                            os.environ["WINDOWS_SIGN_PASSWORD"], "/tr", "http://timestamp.digicert.com",
                            "/td", "SHA256", str(path)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise RuntimeError("Windows signing failed; certificate arguments are withheld") from None
        run([tool, "verify", "/pa", str(path)])
    return True


def installer(
    signed: bool,
    destination: Path,
    ) -> Path:
    """
    Package the verified native bundle as a per-user Windows installer or macOS disk image.

    Args:
        signed (bool): Whether the bundle has an externally verifiable publisher signature.
        destination (Path): Output folder for downloadable installers.

    Returns:
        Path: Newly built installer or disk image.
    """
    label = "signed" if signed else "unsigned"
    if sys.platform == "win32":
        redist = ROOT / "build/redist/MicrosoftEdgeWebview2Setup.exe"
        download("https://go.microsoft.com/fwlink/p/?LinkId=2124703", redist)
        verify_publisher(redist, "O=Microsoft Corporation,")
        run([str(windows_compiler()), f"/DAppVersion={__version__}", f"/DBuildLabel={label}",
             f"/DBundleDir={ROOT / 'dist/piDeck'}", f"/DRedistDir={redist.parent}",
             f"/DOutputPath={destination}", str(ROOT / "desktop/windows.iss")])
        result = destination / f"piDeck-{__version__}-windows-x64-{label}-setup.exe"
        sign_windows([result])
        return result
    result = destination / f"piDeck-{__version__}-macos-{platform.machine()}-{label}.dmg"
    with tempfile.TemporaryDirectory(prefix="pideck-dmg-", dir=ROOT / "build") as temporary:
        stage = Path(temporary)
        shutil.copytree(ROOT / "dist/piDeck.app", stage / "piDeck.app", symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        (stage / "Read me.txt").write_text("Drag piDeck to Applications. Settings contains configuration and startup options.\n"
                                          + ("Unsigned test build: macOS may require an explicit exception to open this app.\n" if not signed else ""))
        run(["hdiutil", "create", "-volname", "piDeck", "-srcfolder", str(stage), "-ov", "-format", "UDZO", str(result)])
    if signed:
        run(["codesign", "--sign", os.environ["MACOS_SIGNING_IDENTITY"], "--timestamp", str(result)])
        profile = os.environ.get("MACOS_NOTARY_PROFILE")
        if not profile:
            raise RuntimeError("Signed Mac distributions require MACOS_NOTARY_PROFILE for notarization")
        run(["xcrun", "notarytool", "submit", str(result), "--keychain-profile", profile, "--wait"])
        run(["xcrun", "stapler", "staple", str(result)])
    return result


# --- Audit, smoke test, and artifact provenance ---


def smoke_test(
    app: Path,
    mode: str,
    report: Path,
    ) -> None:
    """
    Run an isolated bundled self-test and retain actionable diagnostics on failure.

    Args:
        app (Path): Packaged native executable to test.
        mode (str): Backend or native-window test flag.
        report (Path): Non-secret JSON result produced by the isolated test.

    Returns:
        None: The packaged test passed or a diagnostic build failure is raised.
    """
    report.unlink(missing_ok=True)
    try:
        result = subprocess.run([str(app), mode, "--report", str(report)], timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Bundled self-test timed out; inspect the retained test reports") from None
    if result.returncode or not report.is_file() or not json.loads(report.read_text())["ok"]:
        if report.is_file():
            print(report.read_text(encoding="utf-8"), flush=True)
        raise RuntimeError("Bundled self-test failed")


def main() -> None:
    """
    Audit locked build inputs, create a native bundle, smoke-test it, and package an installer.

    Returns:
        None: Installers, dependency audit, and source provenance are written under dist/installers.
    """
    argparse.ArgumentParser(description=__doc__).parse_args()
    if sys.platform not in {"win32", "darwin"}:
        raise RuntimeError("Build desktop installers natively on Windows or macOS")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise RuntimeError("Installer builds require a clean committed checkout")
    destination = ROOT / "dist/installers"
    destination.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "pip_audit", "--local", "--format", "json", "--progress-spinner", "off",
         "--output", str(destination / f"dependency-audit-{sys.platform}-{platform.machine()}.json")])
    if sys.platform == "darwin":
        mac_icon()
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "desktop/piDeck.spec")])
    bundle = ROOT / ("dist/piDeck" if sys.platform == "win32" else "dist/piDeck.app/Contents/MacOS")
    app = bundle / ("piDeck.exe" if sys.platform == "win32" else "piDeck")
    hook = bundle / ("piDeck-hook.exe" if sys.platform == "win32" else "piDeck-hook")
    report = ROOT / "build/bundle-self-test.json"
    smoke_test(app, "--self-test", report)
    smoke_test(app, "--smoke-window", ROOT / "build/native-window-test.json")
    subprocess.run([str(hook), "--source", "cursor"], input='{"hook_event_name":"sessionEnd"}',
                   text=True, check=True, timeout=10, capture_output=True)
    secrets = [path for path in (ROOT / "dist/piDeck").rglob("*") if path.name in {".env", "hook.json", ".credentials.json"}]
    if secrets:
        raise RuntimeError("Private configuration must never be bundled")
    signed = sign_windows([app, hook]) if sys.platform == "win32" else bool(os.environ.get("MACOS_SIGNING_IDENTITY"))
    result = installer(signed, destination)
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    if current != commit or dirty:
        raise RuntimeError("Source changed during the build; discard this candidate and rebuild")
    manifest = {"version": __version__, "source_commit": commit, "dirty_source": dirty,
                "platform": sys.platform, "architecture": platform.machine(), "python": platform.python_version(),
                "publisher_signed": signed, "installer": result.name,
                "lock_sha256": hashlib.sha256((ROOT / "requirements-lock.txt").read_bytes()).hexdigest(),
                "build_dependencies": sorted(f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions())}
    (destination / f"build-manifest-{sys.platform}-{platform.machine()}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                   for path in sorted(destination.iterdir()) if path.is_file() and path.name != "SHA256SUMS")
    (destination / "SHA256SUMS").write_text(sums, encoding="utf-8")
    print(f"Built {result.name}; publisher signed: {signed}")


if __name__ == "__main__":
    main()
