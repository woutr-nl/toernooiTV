#!/usr/bin/env bash
# Post-install / post-reboot health check for the Toernooi TV appliance.
# Safe to run without sudo. Run after:  sudo bash install-kiosk.sh && sudo reboot
#   bash /home/woutr/toernooi-tv/verify-appliance.sh
set -u
pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass+1)); }
bad()  { echo "  ✗ $1"; fail=$((fail+1)); }
note() { echo "    $1"; }

echo "== packages =="
for p in cage chromium comitup avahi-daemon; do
  dpkg -s "$p" >/dev/null 2>&1 && ok "$p installed" || bad "$p NOT installed"
done

echo "== services =="
for u in toernooitv-server toernooitv-kiosk comitup avahi-daemon; do
  systemctl is-active --quiet "$u" && ok "$u active" || bad "$u not active (journalctl -u $u)"
done
systemctl is-enabled --quiet toernooitv-kiosk 2>/dev/null && ok "kiosk enabled at boot" || bad "kiosk not enabled"
if systemctl is-active --quiet getty@tty1; then bad "getty@tty1 still owns tty1 (kiosk needs it)"; else ok "getty@tty1 stepped aside"; fi

echo "== web server =="
for port in 8770 80; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port/health" 2>/dev/null)
  [ "$code" = "200" ] && ok "port $port answers (HTTP 200)" || bad "port $port no answer (got '${code:-none}')"
done
board=$(curl -s "http://localhost:8770/board" 2>/dev/null)
echo "$board" | grep -q '"ok"' && ok "/board responds" || bad "/board did not respond"
echo "$board" | grep -q '"source": *"live"' && note "board source: live (cookie working)" \
  || note "board not live yet — log in at /beheer (this is fine pre-config)"

echo "== mDNS / name =="
# mDNS .local is case-insensitive (RFC 6762), so any-case "toernooitv" is fine.
case "$(hostname | tr '[:upper:]' '[:lower:]')" in
  toernooitv) ok "hostname is toernooitv (case-insensitive)";;
  *) bad "hostname is $(hostname), expected toernooitv";;
esac
if command -v avahi-resolve >/dev/null 2>&1; then
  avahi-resolve -4 -n toernooitv.local >/dev/null 2>&1 && ok "toernooitv.local resolves" || bad "toernooitv.local does not resolve"
else
  note "avahi-resolve not present (install avahi-utils) — skipping name check"
fi
[ -f /etc/avahi/services/toernooitv.service ] && ok "avahi service advertised" || bad "avahi service file missing"

echo "== wifi =="
ssid=$(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '/^yes:/{print $2}')
note "active SSID: ${ssid:-<none / on setup hotspot>}"

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "== summary: $pass passed, $fail failed =="
echo "   open from a phone/laptop:  http://toernooitv.local/beheer   (or http://${ip:-<pi-ip>}/beheer)"
[ "$fail" -eq 0 ] && echo "   all green — check the TV shows the fullscreen display." \
  || echo "   some checks failed — paste this output back for diagnosis."
