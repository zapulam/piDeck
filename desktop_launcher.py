"""Windowed entry point used by the standalone Windows and macOS desktop builds."""
from pideck.desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
