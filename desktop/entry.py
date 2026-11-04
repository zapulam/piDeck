"""

Launch the bundled piDeck desktop application without requiring an installed Python.

"""

from pideck.desktop import main


# --- Native application entry point ---

if __name__ == "__main__":
    raise SystemExit(main())
