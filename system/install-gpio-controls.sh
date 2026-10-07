#!/bin/bash
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this installer as the non-root PiMusic user, without sudo."
  exit 1
fi

if ! /usr/bin/python3 -c 'import gpiozero' >/dev/null 2>&1; then
  echo "Install the GPIO dependency first: sudo apt install python3-gpiozero"
  exit 1
fi

if ! sudo -n /usr/local/bin/pimusic-hotspot-control status >/dev/null 2>&1; then
  echo "Install the fallback-hotspot helper first: sudo ./system/install-hotspot-fallback.sh"
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR="$HOME/.local/lib/pimusic"
UNIT_DIR="$HOME/.config/systemd/user"

install -d -m 755 "$INSTALL_DIR" "$UNIT_DIR"
install -m 755 "$SCRIPT_DIR/pimusic-gpio" "$INSTALL_DIR/pimusic-gpio"
install -m 644 "$SCRIPT_DIR/pimusic-gpio.service" "$UNIT_DIR/pimusic-gpio.service"

systemctl --user daemon-reload
systemctl --user enable --now pimusic-gpio.service

echo "Installed and started pimusic-gpio.service."
echo "Hold the GPIO17 button for three seconds to request the setup hotspot."
