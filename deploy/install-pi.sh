#!/bin/sh
# Install reviewed source as an isolated system service. Run from a trusted checkout with sudo.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
APP=/opt/pideck
STATE=/var/lib/pideck
if ! id pideck >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE" --shell /usr/sbin/nologin pideck
fi
install -d -o root -g root -m 0755 "$APP"
install -d -o pideck -g pideck -m 0700 "$STATE"
# Copy only application files; never copy a developer's .env, profile, or virtual environment.
for directory in pideck hooks; do
  install -d -o root -g root -m 0755 "$APP/$directory"
  cp -R "$SOURCE/$directory/." "$APP/$directory/"
done
install -o root -g root -m 0644 "$SOURCE/requirements.txt" "$APP/requirements.txt"
install -o root -g root -m 0644 "$SOURCE/requirements-pi-lock.txt" "$APP/requirements-pi-lock.txt"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install --require-hashes -r "$APP/requirements-pi-lock.txt"
chown -R root:root "$APP"
chmod -R go-w "$APP"
if [ ! -e "$STATE/config.yaml" ]; then
  install -o pideck -g pideck -m 0600 "$SOURCE/config.yaml" "$STATE/config.yaml"
fi
if [ ! -e "$STATE/.env" ]; then
  install -o pideck -g pideck -m 0600 "$SOURCE/.env.example" "$STATE/.env"
fi
install -o root -g root -m 0644 "$SOURCE/deploy/pideck.service" /etc/systemd/system/pideck.service
systemctl daemon-reload
systemctl enable --now pideck.service
echo "piDeck installed. Private configuration: /var/lib/pideck (use sudo to edit)."
echo "For remote access, configure HTTPS forwarding to 127.0.0.1:8765; never expose raw HTTP."
echo "Use PIDECK_HOOK_TOKEN for agents and PIDECK_TOKEN to sign into the display."
