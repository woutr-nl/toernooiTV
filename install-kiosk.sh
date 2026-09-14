#!/usr/bin/env bash
#
# Turn this Raspberry Pi into a plug-in-HDMI Toernooi TV appliance:
#   - installs a kiosk browser (cage + chromium) and the wifi wizard (comitup)
#   - runs the server on boot (systemd)
#   - launches the fullscreen display on the Pi's HDMI on boot
#   - falls back to a setup hotspot when no known wifi is reachable
#   - installs the on-demand self-updater (toernooitv-update.service)
#
# Run once from the checkout (owned by the box's normal user, path without spaces):
#   sudo bash ./install-kiosk.sh
# Idempotent — safe to re-run. Reboot afterwards.

set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash ./install-kiosk.sh" >&2
  exit 1
fi
case "$APP" in
  *" "*) echo "The checkout path contains a space ($APP) — clone to a path without spaces." >&2; exit 1;;
esac
USER_NAME="$(stat -c %U "$APP")"
if [ "$USER_NAME" = "root" ]; then
  echo "$APP is owned by root: the checkout must be owned by the normal user that will run the box (chown it, or clone as that user)." >&2
  exit 1
fi
USER_UID="$(id -u "$USER_NAME")"
echo "==> Installing for user $USER_NAME (uid $USER_UID) from $APP"

# fill the __APP__/__USER__/__UID__ placeholders of an appliance/ template
render() {
  sed -e "s|__APP__|$APP|g" -e "s|__USER__|$USER_NAME|g" -e "s|__UID__|$USER_UID|g" "$APP/appliance/$1"
}

echo "==> Installing packages (cage, chromium, comitup, avahi, curl, git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y cage chromium comitup curl git avahi-daemon avahi-utils

echo "==> Setting hostname + mDNS so the box is reachable at toernooitv.local…"
hostnamectl set-hostname toernooitv 2>/dev/null || true
install -d /etc/avahi/services
install -m 644 "$APP/appliance/toernooitv.avahi.service" /etc/avahi/services/toernooitv.service

echo "==> Installing systemd units…"
for unit in toernooitv-server.service toernooitv-kiosk.service toernooitv-update.service \
            toernooitv-hotspot-dhcp.service toernooitv-hotspot-dhcp.timer; do
  render "$unit" > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
chmod +x "$APP/appliance/kiosk.sh" "$APP/appliance/hotspot-dhcp.sh"

echo "==> Allowing the server to start the updater (sudoers)…"
tmp="$(mktemp)"
render sudoers > "$tmp"
if ! visudo -c -f "$tmp" >/dev/null; then
  rm -f "$tmp"
  echo "Generated sudoers rule failed validation — not installed. Aborting." >&2
  exit 1
fi
install -m 440 -o root -g root "$tmp" /etc/sudoers.d/toernooitv
rm -f "$tmp"

echo "==> Configuring comitup wifi wizard…"
install -m 644 "$APP/appliance/comitup.conf" /etc/comitup.conf

echo "==> Wifi country NL + radio unblock (a fresh Pi OS ships soft-blocked without a country)…"
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country NL || true
else
  iw reg set NL 2>/dev/null || true
  install -d /etc/modprobe.d
  echo 'options cfg80211 ieee80211_regdom=NL' > /etc/modprobe.d/toernooitv-wifi.conf
fi
rfkill unblock wifi 2>/dev/null || true

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
# HOTSPOT ⇒ dnsmasq watchdog: comitup can miss the callback that starts it (no IPs for phones)
systemctl enable --now toernooitv-hotspot-dhcp.timer
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
echo "   version: $(cat "$APP/VERSION" 2>/dev/null || echo dev)"
echo "   open from another device: http://toernooitv.local/beheer"
echo "   (or by IP: http://$(hostname -I | awk '{print $1}')/beheer)"
echo "   update later with the 'Nu bijwerken' button in Instellingen"
echo
echo "Reboot to start the on-TV kiosk:   sudo reboot"
echo "After reboot the Pi's HDMI should show the fullscreen display."
echo "If it can't find a known wifi, look for the 'ToernooiTV-setup-…' network."
echo "Then check everything with:        bash ./verify-appliance.sh"
