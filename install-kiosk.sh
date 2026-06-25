#!/usr/bin/env bash
#
# Turn this Raspberry Pi into a plug-in-HDMI Toernooi TV appliance:
#   - installs a kiosk browser (cage + chromium) and the wifi wizard (comitup)
#   - runs the server on boot (systemd)
#   - launches the fullscreen display on the Pi's HDMI on boot
#   - falls back to a setup hotspot when no known wifi is reachable
#
# Run once:   sudo bash /home/woutr/toernooi-tv/install-kiosk.sh
# Idempotent — safe to re-run. Reboot afterwards.

set -euo pipefail
APP=/home/woutr/toernooi-tv
USER_NAME=woutr

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash $APP/install-kiosk.sh" >&2
  exit 1
fi

echo "==> Installing packages (cage, chromium, comitup, avahi, curl)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y cage chromium comitup curl avahi-daemon avahi-utils

echo "==> Setting hostname + mDNS so the box is reachable at toernooitv.local…"
hostnamectl set-hostname toernooitv 2>/dev/null || true
install -d /etc/avahi/services
install -m 644 "$APP/appliance/toernooitv.avahi.service" /etc/avahi/services/toernooitv.service

echo "==> Installing systemd units…"
install -m 644 "$APP/appliance/toernooitv-server.service" /etc/systemd/system/
install -m 644 "$APP/appliance/toernooitv-kiosk.service"  /etc/systemd/system/
chmod +x "$APP/appliance/kiosk.sh"

echo "==> Configuring comitup wifi wizard…"
install -m 644 "$APP/appliance/comitup.conf" /etc/comitup.conf

echo "==> Freeing ports 8770/80 if a dev server is holding them…"
fuser -k 8770/tcp 2>/dev/null || true

echo "==> Enabling services…"
systemctl daemon-reload
systemctl enable --now toernooitv-server.service
# the kiosk owns tty1, so the text login there must step aside
systemctl disable --now getty@tty1.service 2>/dev/null || true
systemctl enable toernooitv-kiosk.service
# comitup brings up the setup hotspot when no known wifi is found
systemctl enable comitup.service 2>/dev/null || true
# Mask comitup's own web portal: it binds :80 too, so it fought our server for
# the port and crash-looped (no portal page). Our server serves the wifi setup
# page on :80 instead (captive-redirect + the Wifi card), driving comitup over
# D-Bus. Masking stops the conflict for good.
systemctl disable comitup-web.service 2>/dev/null || true
systemctl mask comitup-web.service 2>/dev/null || true
systemctl enable --now avahi-daemon.service 2>/dev/null || true

echo
echo "==> Done. Quick checks:"
systemctl is-active toernooitv-server.service && echo "   server: active" || echo "   server: NOT active (see: journalctl -u toernooitv-server)"
echo "   open from another device: http://toernooitv.local/beheer"
echo "   (or by IP: http://$(hostname -I | awk '{print $1}')/beheer)"
echo
echo "Reboot to start the on-TV kiosk:   sudo reboot"
echo "After reboot the Pi's HDMI should show the fullscreen display."
echo "If it can't find a known wifi, look for the 'ToernooiTV-setup-…' network."
