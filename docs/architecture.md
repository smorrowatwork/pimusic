# Architecture

## Current implementation

PiMusic is a single FastAPI process (`app.py`) that serves its REST API and the static browser UI (`static/index.html`, `static/app.js`, and `static/style.css`). It is designed to run as a non-root systemd *user* service alongside a persistent non-root MPV user service. The installation commands and unit examples remain in [README.md](../README.md).

```text
Browser on trusted LAN
        | HTTP :8000
        v
FastAPI backend + static UI
   | Navidrome/Subsonic REST           | JSON IPC: /tmp/pimusic-mpv.sock
   v                                  v
Navidrome                         persistent MPV process
                                         |
                                   PipeWire / WirePlumber
                                         |
                              Bluetooth, USB, HDMI, or local audio
```

### Backend responsibilities

- Reads `NAVIDROME_BASE`, `NAVIDROME_USER`, `NAVIDROME_SALT`, and `NAVIDROME_TOKEN` from its environment. The documented source is a private `~/.config/pimusic/pimusic.env` file.
- Proxies Navidrome/Subsonic library, search, cover-art, and streaming requests so credentials are not exposed to the browser.
- Controls one idle MPV process through its Unix-domain JSON IPC socket at `/tmp/pimusic-mpv.sock`, rather than launching a player per track.
- Maintains queue metadata, recovery state, shuffle state, and volume JSON files under `~/.config/pimusic/`. The current code has a hard-coded queue-state base path for `/home/stevenm`; deployment changes should review that assumption.
- On FastAPI startup, restores saved queue and playback state only when MPV has an empty playlist, then restores saved volume.
- Provides Wi-Fi status/scan/connect and saved-profile endpoints through `nmcli`; provides Bluetooth scanning, pairing, trust, connection, and PipeWire sink selection through `bluetoothctl` and `wpctl`.

### Browser/UI responsibilities

The vanilla JavaScript client calls `/api/*` endpoints for playback, queue editing, library browsing, Wi-Fi, Bluetooth, and system status. It polls player status every second. The UI is served from the same FastAPI process, which mounts `static/` at `/`.

### Services and privileged boundary

- The README specifies `pimusic-mpv.service` and `pimusic-web.service` as user units, enabled with `loginctl enable-linger` so they run at boot without an interactive login.
- `system/pimusic-hotspot-fallback.service` is an optional system service. Its installed script repeatedly tries a preferred/saved NetworkManager Wi-Fi profile and starts a `pimusic-setup` shared-IP hotspot if none connects.
- The fallback installer also installs `/usr/local/bin/pimusic-hotspot-control` and grants the PiMusic user passwordless access to that exact helper. The web API uses it only to read fallback status or explicitly enter/exit setup mode; it does not receive broad systemd or NetworkManager `sudo` access.
- The README describes, but this repository does **not** provide, a root-owned `/usr/local/bin/pimusic-set-bluetooth-device` helper and an optional `pimusic-bluetooth.service` reconnect unit. The backend currently calls that helper when saving a selected speaker.

Ports and sockets: the documented web listener is TCP 8000 on all interfaces; MPV IPC is `/tmp/pimusic-mpv.sock`; the documented fallback hotspot normally uses `10.42.0.1` through NetworkManager shared IPv4.

## Planned, not current architecture

The desired provisioning design is a NetworkManager-based state flow shared by automatic Wi-Fi failure, a GPIO button, and the web UI. The repository now has a limited web entry/exit control for the existing fallback hotspot, but GPIO state handling, captive portal behavior, `pimusic.local` mDNS, and the unified provisioning state machine are not present today.
