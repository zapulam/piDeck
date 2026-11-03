"""Version gates, artifact integrity, and contributor-attribution regression tests."""
import hashlib
import json
from types import SimpleNamespace
import sys

import pytest

from pideck import __version__, desktop
from scripts.check_attribution import violations
from scripts.release_metadata import validate_tag, write_checksums


"""# --- Release versions and integrity ---"""


@pytest.mark.parametrize("tag", ["0.1.0", "v0.2.0", "v01.1.0", "v0.1.0/other", "v0.1.0;bad"])
def test_release_tag_must_match_package_version(
        tag,
):
    """Reject mismatched, malformed, or shell-like release references before building."""
    with pytest.raises(ValueError):
        validate_tag(tag, "0.1.0")
    validate_tag("v0.1.0", "0.1.0")


def test_release_checksums_cover_archives_and_detect_changed_content(
        tmp_path,
):
    """Generate standard SHA256SUMS entries for all archives, excluding stale manifests."""
    (tmp_path / "z.zip").write_bytes(b"windows")
    (tmp_path / "a.zip").write_bytes(b"macos")
    (tmp_path / "SHA256SUMS").write_text("outdated")
    lines = write_checksums(tmp_path).read_text().splitlines()
    assert lines == [f"{hashlib.sha256(b'macos').hexdigest()}  a.zip",
                     f"{hashlib.sha256(b'windows').hexdigest()}  z.zip"]
    (tmp_path / "a.zip").write_bytes(b"changed")
    assert write_checksums(tmp_path).read_text().splitlines()[0] != lines[0]


def test_release_checksums_reject_empty_distribution(
        tmp_path,
):
    """An absent build must fail rather than producing a misleading empty manifest."""
    with pytest.raises(ValueError):
        write_checksums(tmp_path)


"""# --- Commit credit validation ---"""


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
        name,
        email,
        message,
):
    """Reject tool/bot authors and bot credit trailers while preserving human ownership."""
    record = "\0".join(["abcdef123456", name, email, "Zachary", "user@example.com", message]) + "\0\0"
    assert violations(record)


@pytest.mark.parametrize("tool, email", [
    ("Claude", "noreply@anthropic.com"),
    ("Codex", "noreply@openai.com"),
    ("Cursor", "cursoragent@cursor.com"),
])
@pytest.mark.parametrize("trailer", ["Co-authored-by", "Signed-off-by"])
def test_attribution_gate_allows_ai_tool_credits(
        tool,
        email,
        trailer,
):
    """Allow tool credit trailers when the primary author and committer remain human."""
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
        committer,
        email,
):
    """A human author and an allowed tool trailer must not conceal a non-human committer."""
    message = "Feature\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    record = "\0".join(["abcdef", "Zachary", "user@example.com", committer, email, message]) + "\0\0"
    assert violations(record)


def test_attribution_gate_allows_product_names_and_human_credits():
    """Mentioning integrated providers in a feature commit must not be treated as attribution."""
    message = "Add Claude and Codex usage support\n\nCo-authored-by: Another Human <human@example.com>"
    record = "\0".join(["abcdef", "Zachary", "user@example.com", "GitHub", "noreply@github.com", message]) + "\0\0"
    assert violations(record) == []


"""# --- Frozen app self-test entry point ---"""


def test_headless_package_validation_reports_backend_assets_and_version(
        tmp_path,
        monkeypatch,
):
    """The CI entry point checks the actual backend and assets without requiring a GUI."""
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    report = tmp_path / "report.json"
    assert desktop.main(["--self-test", "--report", str(report)]) == 0
    result = json.loads(report.read_text())
    assert result["ok"] and result["version"] == __version__
    assert {"ui-assets", "agent-event", "shutdown"} <= set(result["checks"])
