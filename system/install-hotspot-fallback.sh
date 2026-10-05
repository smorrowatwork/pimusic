#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo."
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WIFI_DEVICE=${WIFI_DEVICE:-wlan0}
PREFERRED_CONNECTION=${PREFERRED_CONNECTION:-netplan-wlan0-morrow}
HOTSPOT_CONNECTION=pimusic-setup
HOTSPOT_SSID=pimusic-setup
PIMUSIC_USER=${PIMUSIC_USER:-${SUDO_USER:-}}

if [[ ! "$PIMUSIC_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || ! id "$PIMUSIC_USER" >/dev/null 2>&1; then
  echo "Set PIMUSIC_USER to the non-root account that runs PiMusic."
  exit 1
fi

read -r -s -p "Hotspot password for ${HOTSPOT_SSID}: " HOTSPOT_PASSWORD
echo
if [[ ${#HOTSPOT_PASSWORD} -lt 8 || ${#HOTSPOT_PASSWORD} -gt 63 ]]; then
  echo "The hotspot password must be 8 to 63 characters."
  exit 1
fi

install -d -m 755 /usr/local/lib/pimusic
install -m 755 "$SCRIPT_DIR/pimusic-hotspot-fallback" \
  /usr/local/lib/pimusic/pimusic-hotspot-fallback
install -m 755 "$SCRIPT_DIR/pimusic-hotspot-control" \
  /usr/local/bin/pimusic-hotspot-control
install -m 644 "$SCRIPT_DIR/pimusic-hotspot-fallback.service" \
  /etc/systemd/system/pimusic-hotspot-fallback.service

umask 077
cat > /etc/pimusic-hotspot.nmconnection <<EOF
[connection]
id=${HOTSPOT_CONNECTION}
uuid=491e9f62-8b6e-4e72-9f51-1e4d9648c47b
type=wifi
interface-name=${WIFI_DEVICE}
autoconnect=false

[wifi]
mode=ap
ssid=${HOTSPOT_SSID}
band=bg

[wifi-security]
key-mgmt=wpa-psk
psk=${HOTSPOT_PASSWORD}

[ipv4]
method=shared

[ipv6]
method=disabled
EOF
install -d -m 755 /etc/NetworkManager/system-connections
install -m 600 /etc/pimusic-hotspot.nmconnection \
  /etc/NetworkManager/system-connections/${HOTSPOT_CONNECTION}.nmconnection
rm -f /etc/pimusic-hotspot.nmconnection

install -d -m 755 /etc/pimusic
cat > /etc/pimusic/hotspot-fallback.conf <<EOF
WIFI_DEVICE=${WIFI_DEVICE}
PREFERRED_CONNECTION=${PREFERRED_CONNECTION}
HOTSPOT_CONNECTION=${HOTSPOT_CONNECTION}
CONNECT_TIMEOUT=30
RETRY_INTERVAL=30
EOF
chmod 600 /etc/pimusic/hotspot-fallback.conf

printf '%s ALL=(root) NOPASSWD: /usr/local/bin/pimusic-hotspot-control\n' "$PIMUSIC_USER" \
  > /etc/sudoers.d/pimusic-hotspot-control
chmod 440 /etc/sudoers.d/pimusic-hotspot-control
visudo -cf /etc/sudoers.d/pimusic-hotspot-control

nmcli connection reload
systemctl daemon-reload
systemctl enable pimusic-hotspot-fallback.service

echo "Installed. Start it after your next normal Wi-Fi test with:"
echo "  sudo systemctl start pimusic-hotspot-fallback.service"
