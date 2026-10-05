# PiMusic agent guide

## Purpose and platform

PiMusic is a small, headless, browser-controlled music appliance for a Raspberry Pi Zero 2 W running a Debian-based system. It streams a Navidrome/Subsonic library through MPV to local audio outputs, especially Bluetooth speakers. Treat it as a consumer appliance, not a general-purpose server: it should require little technical interaction from its user.

## Current design

- `app.py` is the FastAPI backend and serves the static browser application in `static/`.
- Navidrome access is server-side; credentials come from `~/.config/pimusic/pimusic.env` and must never reach the browser or repository.
- A long-running user `mpv` process exposes `/tmp/pimusic-mpv.sock`; the backend controls it with JSON IPC and persists queue, recovery, shuffle, and volume data in `~/.config/pimusic/`.
- NetworkManager (`nmcli`) manages Wi-Fi. Bluetooth management uses `bluetoothctl`; PipeWire output selection uses `wpctl`.
- Runtime is intended to be systemd-managed: user services for MPV and the web backend, plus the optional system `pimusic-hotspot-fallback.service` in `system/`.

Read [README.md](README.md), [docs/architecture.md](docs/architecture.md), [docs/hardware.md](docs/hardware.md), and [docs/development-history.md](docs/development-history.md) before changing major behavior. Also inspect the relevant code and systemd/configuration files.

## Constraints and working rules

- Target is a resource-constrained Pi Zero 2 W: favor lightweight dependencies and incremental changes.
- Preserve working playback, queue recovery, and Bluetooth behavior. Do not replace working components unnecessarily.
- The web service must not run as root. Privileged work should be isolated behind narrowly scoped helpers/services; do not expand broad `sudo` access.
- Never commit credentials, Wi-Fi passwords, private keys, or generated private configuration.
- Before changing networking, audio, startup, or permissions, inspect `app.py`, `system/`, the README, and the applicable docs.
- Propose and discuss broad architectural rewrites before implementing them.

## Current direction

Wi-Fi provisioning is the next major area. The existing first slice lets the web UI explicitly enter or exit the NetworkManager fallback hotspot through a constrained root helper, scan/select a network, and return safe connection feedback when the setup hotspot disconnects the browser. It has been manually tested end-to-end. The remaining direction is shared provisioning state for automatic fallback, a physical button, and the web UI. See the planned sections of the hardware and history docs; do not mistake them for implemented GPIO or captive-portal behavior.
