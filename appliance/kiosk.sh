#!/bin/sh
# Launched by cage (Wayland kiosk compositor) as the single fullscreen client.
# Waits for the local server, then opens the fullscreen display in Chromium.
APP="$(cd "$(dirname "$0")/.." && pwd)"
URL="http://localhost:8770/"

# Cage draws its left_ptr arrow at screen centre from the xcursor theme
# "default" (~/.icons first in the lookup path) and has no option to hide it.
# Point the user's default cursor theme at the transparent one in the repo.
# Cage loaded its theme before this script ran, so right after installing
# the link, exit: systemd (Restart=always) starts cage again with the theme
# in place. Chromium below keeps the stock system lookup via XCURSOR_PATH,
# so a plugged-in mouse still shows a real pointer (the page idles it away).
THEME="$APP/appliance/hidden-cursor-theme"
if [ "$(readlink "$HOME/.icons/default" 2>/dev/null)" != "$THEME" ]; then
  mkdir -p "$HOME/.icons" 2>/dev/null
  ln -sfn "$THEME" "$HOME/.icons/default" 2>/dev/null
  if [ "$(readlink "$HOME/.icons/default" 2>/dev/null)" = "$THEME" ]; then
    echo "kiosk: hidden cursor theme installed; restarting so cage picks it up"
    exit 0
  fi
fi
export XCURSOR_PATH=/usr/share/icons:/usr/share/pixmaps

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
