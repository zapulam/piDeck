"""

Launch the bundled piDeck desktop application without requiring an installed Python.

"""

from pideck.desktop import main
import json
from pathlib import Path
import sys
import traceback


# --- Native application entry point ---

if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        if ("--self-test" in sys.argv or "--smoke-window" in sys.argv) and "--report" in sys.argv:
            report = Path(sys.argv[sys.argv.index("--report") + 1])
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({"ok": False, "error": traceback.format_exc()}), encoding="utf-8")
            result = 1
        else:
            raise
    raise SystemExit(result)
