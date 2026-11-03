"""Windowed entry point used by the standalone Windows and macOS desktop builds."""
from pideck.desktop import main
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
