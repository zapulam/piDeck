"""

Require human commit identities while allowing AI tool credit trailers.

"""

from __future__ import annotations

import re
import subprocess

BLOCKED_IDENTITIES = re.compile(r"\b(?:claude|anthropic|codex|openai|cursor|dependabot)\b|\[bot\]", re.IGNORECASE)
BLOCKED_CREDITS = re.compile(r"\bdependabot\b|\[bot\]", re.IGNORECASE)
TRAILER = re.compile(r"^(?:Co-authored-by|Signed-off-by):", re.IGNORECASE)


# --- Attribution validation ---


def violations(
    history: str,
    ) -> list[str]:
    """
    Reject tool/bot identities and bot credits while allowing human and AI tool credits.

    Args:
        history (str): NUL-delimited git log records with hash, author, committer, and message.

    Returns:
        list[str]: Descriptions of invalid identities or bot credit trailers, keyed by commit hash.
    """
    found = []
    for record in history.split("\0\0"):
        if not record.strip():
            continue
        sha, author, email, committer, committer_email, message = record.lstrip("\n").split("\0", 5)
        if BLOCKED_IDENTITIES.search(f"{author} {email} {committer} {committer_email}"):
            found.append(f"{sha[:12]}: bot or AI author/committer")
        if any(TRAILER.match(line) and BLOCKED_CREDITS.search(line) for line in message.splitlines()):
            found.append(f"{sha[:12]}: bot credit trailer")
    return found


def main() -> int:
    """
    Inspect HEAD's complete history and return one if unwanted attribution is present.

    Returns:
        int: Zero for clean attribution, otherwise one after printing the affected commits.
    """
    history = subprocess.check_output([
        "git", "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00%x00", "HEAD",
    ], text=True, encoding="utf-8")
    found = violations(history)
    print("\n".join(found) if found else "Commit attribution check passed.")
    return int(bool(found))


if __name__ == "__main__":
    raise SystemExit(main())
