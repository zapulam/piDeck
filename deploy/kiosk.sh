#!/bin/sh
# Fullscreen browser pointed at the local server. Run from your desktop session's autostart.
URL="${PIDECK_URL:-http://localhost:8765}"
until curl -fs "$URL/api/health" >/dev/null; do sleep 1; done
exec chromium --kiosk --noerrdialogs --disable-infobars --disable-pinch \
  --overscroll-history-navigation=0 --check-for-update-interval=31536000 \
  --password-store=basic "$URL"
