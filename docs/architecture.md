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
- Provides a local configuration endpoint that accepts a replacement Navidrome/Subsonic server address, username, and password. It generates a fresh salt and Subsonic token on the Pi, validates them, then atomically writes only the derived credentials to that same private file. It never returns stored credentials or saves the submitted password, and a successful switch pauses playback and clears the queue before applying the new connection.
- Proxies Navidrome/Subsonic library, search, cover-art, and streaming requests so credentials are not exposed to the browser.
- Controls one idle MPV process through its Unix-domain JSON IPC socket at `/tmp/pimusic-mpv.sock`, rather than launching a player per track.
- Maintains queue metadata, recovery state, shuffle state, and volume JSON files under the service user's `~/.config/pimusic/` directory.
- On FastAPI startup, restores saved queue and playback state only when MPV has an empty playlist, then restores saved volume.
- Provides Wi-Fi status/scan/connect and saved-profile endpoints through `nmcli`; connection failures are mapped to credential-safe, actionable messages rather than raw command output. A successful connection response includes the active Wi-Fi address when it can reach the browser. Bluetooth scanning, pairing, trust, connection, and PipeWire sink selection use `bluetoothctl` and `wpctl`.

### Browser/UI responsibilities

The vanilla JavaScript client calls `/api/*` endpoints for playback, queue editing, library browsing, Wi-Fi, Bluetooth, and system status. It polls player status every second. During setup-hotspot handoff, it treats loss of the browser connection after submission as an expected outcome and tells the user to rejoin normal Wi-Fi; confirmed API failures remain visible as specific messages. The UI is served from the same FastAPI process, which mounts `static/` at `/`.

### Services and privileged boundary

- The README specifies `pimusic-mpv.service` and `pimusic-web.service` as user units, enabled with `loginctl enable-linger` so they run at boot without an interactive login.
- `system/pimusic-hotspot-fallback.service` is an optional system service. Its installed script repeatedly tries a preferred/saved NetworkManager Wi-Fi profile and starts a `pimusic-setup` shared-IP hotspot if none connects.
- The fallback installer also installs `/usr/local/bin/pimusic-hotspot-control` and grants the PiMusic user passwordless access to that exact helper. The helper is the current provisioning-status source: it reports `READY`, `TRYING_WIFI`, `PROVISIONING`, or `CONNECTING_WIFI` from NetworkManager and a root-owned runtime marker. The web API uses it only to read that status or explicitly enter/exit setup mode; it does not receive broad systemd or NetworkManager `sudo` access.
- `system/install-bluetooth-reconnect.sh` installs the root-owned `/usr/local/bin/pimusic-set-bluetooth-device` helper, a narrowly scoped sudoers rule for the web-service user, and `pimusic-bluetooth.service`. The helper validates and privately saves the selected MAC; the service retries its BlueZ connection at boot.

Ports and sockets: the documented web listener is TCP 8000 on all interfaces; MPV IPC is `/tmp/pimusic-mpv.sock`; the documented fallback hotspot normally uses `10.42.0.1` through NetworkManager shared IPv4.

## Planned, not current architecture

The desired provisioning design is a NetworkManager-based state flow shared by automatic Wi-Fi failure, a GPIO button, and the web UI. The repository now has a lightweight shared status contract, but GPIO state handling, captive portal behavior, and a full event-driven provisioning state machine are not present today. Normal-network discovery uses the device hostname's mDNS name (for example, `pimusic.local`). The unimplemented work is tracked in the [project backlog](development-history.md#backlog).

### Browser listening mode

Browser listening is an explicit, exclusive output mode. The persistent MPV playlist remains the queue executor and recovery source, while the backend persists browser-output state and keeps the paused MPV playlist at the browser's current track and position. The browser therefore renders the same queue, rather than keeping an independent playlist.

Only one renderer may play at a time. When the user switches from **Pi speaker** to **This browser**, PiMusic captures the canonical playback state, pauses MPV, and instructs the selected browser to begin the same song at the captured position if it was playing. The reverse switch pauses the browser first, then resumes MPV at that position. A failed handoff leaves both renderers paused and reports the failure; PiMusic must never silently create simultaneous audio.

The browser will request audio only from a PiMusic streaming endpoint. The backend will proxy Navidrome audio, including byte-range requests needed for seeking, so Navidrome credentials and authenticated stream URLs never reach the browser. Browser volume is local to the listening device and must not overwrite the Pi's persisted MPV volume.

The browser that selected **This browser** holds a five-minute playback lease renewed while the page is active and immediately when it returns to the foreground. The window accommodates mobile/browser timer throttling while a listening tab is in the background. Other open PiMusic pages remain controllers of the same queue but cannot start their own audio. If the lease expires because the listening page stays closed or disconnected, playback pauses rather than unexpectedly returning to a Pi speaker. After a Pi restart, the queue is recovered but browser mode is not: the appliance starts with its Pi renderer selected and paused until a user chooses an output.

The backend owns the output route and bounded browser lease. A later internal refactor may make its queue representation independently authoritative, but the current design already preserves one queue and one active renderer without duplicating stream credentials or audio.
