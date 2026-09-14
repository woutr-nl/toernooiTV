#!/bin/sh
# Launched by cage (Wayland kiosk compositor) as the single fullscreen client.
# Waits for the local server, then opens the fullscreen display in Chromium.
APP="$(cd "$(dirname "$0")/.." && pwd)"
URL="http://localhost:8770/"

# Wait up to 60s for the server to answer (it starts in parallel on boot).
i=0
while [ "$i" -lt 60 ]; do
  if curl -sf -o /dev/null "$URL"; then break; fi
  i=$((i + 1))
  sleep 1
done

exec chromium \
  --kiosk --app="$URL" \
  --ozone-platform=wayland \
  --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=Translate,TranslateUI \
  --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required \
  --user-data-dir="$APP/.kiosk-profile"
