#!/usr/bin/env bash
# Invariant: comitup HOTSPOT mode ⇒ a dnsmasq serves DHCP/DNS on wlan0.
# comitup 1.43 relies on a NetworkManager StateChanged callback to start it; that
# callback can be missed around boot (root cause unconfirmed), leaving phones on
# the setup hotspot without an IP. Run as root every 30s by
# toernooitv-hotspot-dhcp.timer.
#
# The dnsmasq runs in its own transient unit: started directly from this oneshot
# it would be killed with the unit's cgroup the moment this script exits. It uses
# comitup's own conf (which sets comitup's pid-file), so comitup's CONNECTING
# callback still stops it as usual.
# wlan0 and the ToernooiTV-setup prefix must match appliance/comitup.conf.
set -u
CONF=/usr/share/comitup/dns/dns-hotspot.conf
ap="$(nmcli -t -f GENERAL.CONNECTION dev show wlan0 2>/dev/null | cut -d: -f2-)"
case "$ap" in
  ToernooiTV-setup*)
    if ! pgrep -f "dnsmasq.*dns-hotspot.conf" >/dev/null 2>&1; then
      logger -t toernooitv-hotspot-dhcp "hotspot $ap up without dnsmasq — starting it"
      systemd-run --unit=toernooitv-hotspot-dnsmasq --collect \
        /usr/sbin/dnsmasq --keep-in-foreground --conf-file="$CONF" --interface=wlan0 \
        || logger -t toernooitv-hotspot-dhcp "starting dnsmasq failed"
    fi ;;
esac
exit 0
