"""

Lock an owned desktop profile and reactivate it using an operation-only private credential.

"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
from typing import Any, Iterator
import urllib.request

from hooks.pideck_hook import protect_file, safe_open


# --- Profile ownership and safe reactivation ---


@contextmanager
def desktop_instance(
    config: Path,
    ) -> Iterator[dict[str, Any] | None]:
    """
    Own the profile lock or request activation without disclosing an administrator token.

    Args:
        config (Path): User-owned desktop configuration identifying this instance.

    Returns:
        Iterator[dict[str, Any] | None]: Window metadata for the owner, or None after reactivation.
    """
    directory = config.resolve().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / ".instance.lock"
    metadata = directory / ".instance.json"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    owned = False
    try:
        protect_file(lock)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owned = True
        except OSError:
            try:
                saved = json.loads(metadata.read_text(encoding="utf-8"))
                request = urllib.request.Request(saved["url"] + "api/desktop/activate", data=b"",
                                                 headers={"Authorization": "Bearer " + saved["activation"]})
                with safe_open(request, timeout=3) as response:
                    if response.status != 200:
                        raise ValueError("Activation was refused")
            except Exception:
                raise RuntimeError("piDeck is already running or starting; use its existing window") from None
            yield None
            return
        metadata.unlink(missing_ok=True)
        yield {"activation": secrets.token_urlsafe(32), "instance_path": metadata}
    finally:
        if owned:
            metadata.unlink(missing_ok=True)
        os.close(descriptor)
