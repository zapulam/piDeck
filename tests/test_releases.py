"""

Version gates, artifact integrity, and contributor-attribution regression tests.

"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from pideck import __version__, desktop
from scripts.check_attribution import violations
from scripts.release_metadata import validate_tag, write_checksums


# --- Release versions and integrity ---


@pytest.mark.parametrize("tag", ["0.1.0", "v0.2.0", "v01.1.0", "v0.1.0/other", "v0.1.0;bad"])
def test_release_tag_must_match_package_version(
    tag: str,
    ) -> None:
    """
    Reject mismatched, malformed, or shell-like release references before building.

    Args:
        tag (str): Version tag supplied for validation.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with pytest.raises(ValueError):
        validate_tag(tag, "0.1.0")
    validate_tag("v0.1.0", "0.1.0")


def test_release_checksums_cover_archives_and_detect_changed_content(
    tmp_path: Path,
    ) -> None:
    """
    Generate standard SHA256SUMS entries for all archives, excluding stale manifests.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    (tmp_path / "z.zip").write_bytes(b"windows")
    (tmp_path / "a.zip").write_bytes(b"macos")
    (tmp_path / "SHA256SUMS").write_text("outdated")
    lines = write_checksums(tmp_path).read_text().splitlines()
    assert lines == [f"{hashlib.sha256(b'macos').hexdigest()}  a.zip",
                     f"{hashlib.sha256(b'windows').hexdigest()}  z.zip"]
    (tmp_path / "a.zip").write_bytes(b"changed")
    assert write_checksums(tmp_path).read_text().splitlines()[0] != lines[0]


def test_release_checksums_reject_empty_distribution(
    tmp_path: Path,
    ) -> None:
    """
    An absent build must fail rather than producing a misleading empty manifest.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    with pytest.raises(ValueError):
        write_checksums(tmp_path)


# --- Commit credit validation ---


@pytest.mark.parametrize("name, email, message", [
    ("Claude", "noreply@anthropic.com", "Feature"),
    ("Codex", "noreply@openai.com", "Feature"),
    ("Cursor", "cursoragent@cursor.com", "Feature"),
    ("dependabot[bot]", "bot@example.com", "Update dependency"),
    ("Zachary", "user@example.com", "Feature\n\nCo-authored-by: dependabot[bot] <bot@example.com>"),
    ("Zachary", "user@example.com", "Feature\n\nSigned-off-by: dependabot[bot] <bot@example.com>"),
    ("Zachary", "user@example.com", "Feature\n\nCo-authored-by: automation[bot] <bot@example.com>"),
])
def test_attribution_gate_rejects_tool_identities_and_bot_credits(
    name: str,
    email: str,
    message: str,
    ) -> None:
    """
    Reject tool/bot authors and bot credit trailers while preserving human ownership.

    Args:
        name (str): Contributor identity or public label used by the test case.
        email (str): Contributor email address used by the attribution test.
        message (str): Commit message or event text supplied by the test case.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    record = "\0".join(["abcdef123456", name, email, "Zachary", "user@example.com", message]) + "\0\0"
    assert violations(record)


@pytest.mark.parametrize("tool, email", [
    ("Claude", "noreply@anthropic.com"),
    ("Codex", "noreply@openai.com"),
    ("Cursor", "cursoragent@cursor.com"),
])
@pytest.mark.parametrize("trailer", ["Co-authored-by", "Signed-off-by"])
def test_attribution_gate_allows_ai_tool_credits(
    tool: str,
    email: str,
    trailer: str,
    ) -> None:
    """
    Allow tool credit trailers when the primary author and committer remain human.

    Args:
        tool (str): AI tool name used in an allowed contribution credit.
        email (str): Contributor email address used by the attribution test.
        trailer (str): Commit trailer field whose attribution policy is checked.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    message = f"Feature\n\n{trailer}: {tool} <{email}>"
    record = "\0".join(["abcdef", "Zachary", "user@example.com", "Zachary", "user@example.com", message]) + "\0\0"
    assert violations(record) == []


@pytest.mark.parametrize("committer, email", [
    ("Claude", "noreply@anthropic.com"),
    ("Codex", "noreply@openai.com"),
    ("Cursor", "cursoragent@cursor.com"),
    ("automation[bot]", "bot@example.com"),
])
def test_attribution_gate_rejects_tool_or_bot_committers(
    committer: str,
    email: str,
    ) -> None:
    """
    A human author and an allowed tool trailer must not conceal a non-human committer.

    Args:
        committer (str): Committer identity used by the attribution test.
        email (str): Contributor email address used by the attribution test.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    message = "Feature\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    record = "\0".join(["abcdef", "Zachary", "user@example.com", committer, email, message]) + "\0\0"
    assert violations(record)


def test_attribution_gate_allows_product_names_and_human_credits() -> None:
    """
    Mentioning integrated providers in a feature commit must not be treated as attribution.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    message = "Add Claude and Codex usage support\n\nCo-authored-by: Another Human <human@example.com>"
    record = "\0".join(["abcdef", "Zachary", "user@example.com", "GitHub", "noreply@github.com", message]) + "\0\0"
    assert violations(record) == []


# --- Desktop backend self-test and setup scripts ---


def test_headless_desktop_validation_reports_backend_assets_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ) -> None:
    """
    The desktop self-test checks the backend and assets without requiring a GUI.

    Args:
        tmp_path (Path): Temporary directory owned and cleaned up by pytest.
        monkeypatch (pytest.MonkeyPatch): Pytest helper that restores patched attributes,
            environment, and paths.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    report = tmp_path / "report.json"
    assert desktop.main(["--self-test", "--report", str(report)]) == 0
    result = json.loads(report.read_text())
    assert result["ok"] and result["version"] == __version__
    assert {"ui-assets", "agent-event", "shutdown"} <= set(result["checks"])


def test_desktop_setup_scripts_are_present() -> None:
    """
    Windows and macOS installs are driven by committed setup scripts, not binary ZIPs.

    Returns:
        None: Assertions or fixture setup completed successfully.
    """
    root = Path(__file__).resolve().parents[1]
    windows = (root / "scripts" / "setup-windows.ps1").read_text(encoding="utf-8")
    macos = (root / "scripts" / "setup-macos.sh").read_text(encoding="utf-8")
    assert "requirements-desktop.txt" in windows and "run-desktop.cmd" in windows
    assert "requirements-desktop.txt" in macos and "run-desktop" in macos
    assert "hooks" in windows and "hooks" in macos
    assert "--config" in windows and "--config" in macos
    assert "Startup" in windows and "--startup" in macos
    assert "Start menu" in windows and "piDeck.app" in macos
