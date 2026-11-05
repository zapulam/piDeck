"""

Run bundled agent hooks with working standard streams and a trusted HTTPS certificate bundle.

"""

import os
from pathlib import Path
import sys

import certifi
from hooks.pideck_hook import main


# --- Standalone hook entry point ---

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
if len(sys.argv) > 1 and Path(sys.argv[1]).name == "pideck_hook.py":
    sys.argv = sys.argv[1:]
if __name__ == "__main__":
    raise SystemExit(main())
