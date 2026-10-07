#!/bin/bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo."
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PIMUSIC_USER=${PIMUSIC_USER:-${SUDO_USER:-}}

if [[ ! "$PIMUSIC_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || ! id "$PIMUSIC_USER" >/dev/null 2>&1; then
  echo "Set PIMUSIC_USER to the non-root account that runs PiMusic."
  exit 1
fi

install -d -m 755 /usr/local/lib/pimusic
install -d -m 755 /etc/pimusic
install -m 755 "$SCRIPT_DIR/pimusic-set-bluetooth-device" \
  /usr/local/bin/pimusic-set-bluetooth-device
install -m 755 "$SCRIPT_DIR/pimusic-bluetooth-reconnect" \
  /usr/local/lib/pimusic/pimusic-bluetooth-reconnect
install -m 644 "$SCRIPT_DIR/pimusic-bluetooth.service" \
  /etc/systemd/system/pimusic-bluetooth.service

printf '%s ALL=(root) NOPASSWD: /usr/local/bin/pimusic-set-bluetooth-device\n' "$PIMUSIC_USER" \
  > /etc/sudoers.d/pimusic-set-bluetooth-device
chmod 440 /etc/sudoers.d/pimusic-set-bluetooth-device
visudo -cf /etc/sudoers.d/pimusic-set-bluetooth-device

systemctl daemon-reload
systemctl enable pimusic-bluetooth.service

echo "Installed. Select a Bluetooth speaker in PiMusic to save it for boot-time reconnect."
echo "To test an already saved speaker now:"
echo "  sudo systemctl start pimusic-bluetooth.service"
