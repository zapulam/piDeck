"""Validate version tags and write SHA-256 checksums for release archives."""
import argparse
import ast
import hashlib
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(?:alpha|beta|rc)\.[1-9]\d*)?")


"""# --- Version and artifact helpers ---"""


def project_version(
        root: Path = ROOT,
) -> str:
    """Read the literal package version without importing the application.

    Args:
        root: Repository directory containing pideck/__init__.py.

    Returns:
        The package's declared version string.

    Raises:
        ValueError: No literal string version is declared.
    """
    module = ast.parse((root / "pideck" / "__init__.py").read_text(encoding="utf-8"))
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__version__" for target in statement.targets):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ValueError("pideck/__init__.py must declare a literal __version__ string")


def validate_tag(
        tag: str,
        version: str,
) -> None:
    """Require a semantic version tag that exactly matches the packaged application.

    Args:
        tag: Git release tag, including its v prefix.
        version: Package version declared in pideck/__init__.py.

    Raises:
        ValueError: The tag is malformed or differs from the package version.
    """
    if not TAG.fullmatch(tag) or tag != "v" + version:
        raise ValueError(f"Release tag must be v{version}; received {tag!r}")


def write_checksums(
        directory: Path,
) -> Path:
    """Write a sorted checksum manifest covering every release ZIP in a directory.

    Args:
        directory: Folder containing the desktop and source release archives.

    Returns:
        The path to the generated SHA256SUMS manifest.

    Raises:
        ValueError: No release archives are present.
    """
    archives = sorted(directory.glob("*.zip"))
    if not archives:
        raise ValueError("No release ZIP files were found")
    lines = []
    for path in archives:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    manifest = directory / "SHA256SUMS"
    manifest.write_text("".join(lines), encoding="utf-8", newline="\n")
    return manifest


"""# --- Command entry point ---"""


def main() -> None:
    """Validate a checked-out release tag or generate checksums for prepared assets."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--tag", required=True)
    checksums = commands.add_parser("checksums")
    checksums.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate":
        validate_tag(args.tag, project_version())
        tag_sha = subprocess.check_output(["git", "rev-parse", f"refs/tags/{args.tag}^{{commit}}"], text=True).strip()
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if tag_sha != head:
            raise ValueError("Release checkout does not match the requested tag")
        print(f"Validated {args.tag} at {head}")
    else:
        print(write_checksums(args.directory))


if __name__ == "__main__":
    main()
