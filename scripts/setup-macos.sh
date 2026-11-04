#!/usr/bin/env bash
# Create a local macOS desktop environment for piDeck.
# Clones are the supported Mac install path: venv, desktop deps, run-desktop, optional hooks.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NO_HOOKS=0
STARTUP=0
URL="http://localhost:8765"
TOKEN=""
PYTHON_BIN=""

usage() {
  cat <<'EOF'
Usage: scripts/setup-macos.sh [--no-hooks] [--startup] [--url URL] [--token TOKEN] [--python PATH]

  --no-hooks     Skip Claude Code / Codex / Cursor hook installation
  --startup      Open piDeck at login
  --url URL      Server address for hooks (default http://localhost:8765)
  --token TOKEN  Optional PIDECK_TOKEN when pointing hooks at a remote Pi
  --python PATH  Explicit python3 executable (default: python3 on PATH)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-hooks) NO_HOOKS=1; shift ;;
    --startup) STARTUP=1; shift ;;
    --url) URL="${2:?}"; shift 2 ;;
    --token) TOKEN="${2:?}"; shift 2 ;;
    --python) PYTHON_BIN="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -n "$PYTHON_BIN" ]]; then
  PYTHON=("$PYTHON_BIN")
else
  PYTHON=(python3)
fi

if ! "${PYTHON[@]}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11+ is required. Install it (for example via https://www.python.org/downloads/) and re-run." >&2
  exit 1
fi

echo "Using: ${PYTHON[*]} ($("${PYTHON[@]}" -c 'import sys; print(sys.version.split()[0])'))"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv ..."
  "${PYTHON[@]}" -m venv .venv
fi

echo "Installing desktop dependencies ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-desktop.txt

cat > run-desktop <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "\$(dirname "\$0")"
exec .venv/bin/python -m pideck.desktop --config "$ROOT/config.yaml" "\$@"
EOF
chmod +x run-desktop
echo "Wrote $ROOT/run-desktop"

app="$HOME/Applications/piDeck.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$ROOT/pideck/assets/pideck.png" "$app/Contents/Resources/pideck.png"
if command -v sips >/dev/null && command -v iconutil >/dev/null; then
  iconset="$app/Contents/Resources/pideck.iconset"
  mkdir -p "$iconset"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ROOT/pideck/assets/pideck.png" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  done
  iconutil -c icns "$iconset" -o "$app/Contents/Resources/pideck.icns" || true
  rm -rf "$iconset"
fi
cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>piDeck</string>
  <key>CFBundleDisplayName</key><string>piDeck</string>
  <key>CFBundleIdentifier</key><string>com.zapulam.pideck</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>piDeck</string>
  <key>CFBundleIconFile</key><string>pideck</string>
</dict></plist>
EOF
cat > "$app/Contents/MacOS/piDeck" <<EOF
#!/bin/bash
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m pideck.desktop --config "$ROOT/config.yaml"
EOF
chmod +x "$app/Contents/MacOS/piDeck"
echo "Applications: $app"

if [[ "$STARTUP" -eq 1 ]]; then
  plist="$HOME/Library/LaunchAgents/com.zapulam.pideck.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.zapulam.pideck</string>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>-m</string>
    <string>pideck.desktop</string>
    <string>--config</string>
    <string>$ROOT/config.yaml</string>
  </array>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/com.zapulam.pideck" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  echo "Opens at login: $plist"
fi

if [[ "$NO_HOOKS" -eq 0 ]]; then
  echo "Installing agent hooks for $URL ..."
  hook_args=(hooks/install.py --url "$URL" --no-check)
  if [[ -n "$TOKEN" ]]; then
    hook_args+=(--token "$TOKEN")
  fi
  if .venv/bin/python "${hook_args[@]}"; then
    echo "Hooks installed. Restart Claude Code / Codex / Cursor sessions."
  else
    echo "Hook install reported an error. Re-run after fixing agents, or pass --no-hooks." >&2
  fi
fi

cat <<EOF

Setup complete.
  Start the window:  open "$HOME/Applications/piDeck.app"
  Demo data:         ./run-desktop --mock
  Or open a browser: .venv/bin/python -m pideck --mock
                     then visit http://localhost:8765
  Confirm hooks:     .venv/bin/python hooks/install.py --url $URL --test
EOF

case "$URL" in
  http://localhost*|http://127.0.0.1*)
    echo "  Local desktop hooks do not need a token."
    ;;
esac
