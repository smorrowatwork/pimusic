# PiMusic

PiMusic turns a Raspberry Pi Zero 2 W into a headless, browser-controlled music appliance. It streams a [Navidrome](https://www.navidrome.org/) library through MPV and plays audio on the Pi through PipeWire, Bluetooth, USB, HDMI, or another local audio output.

The interface supports search, playlists, album playback, queue reordering, Play Next, Append, Mix into Queue, Shuffle Upcoming, Wi-Fi setup, Bluetooth speaker selection, queue recovery, and persistent volume.

For durable project context beyond setup and operation, see [architecture](docs/architecture.md), [hardware assumptions](docs/hardware.md), and [development history](docs/development-history.md). Contributors using Codex should start with [AGENTS.md](AGENTS.md).

## Quick Install

These steps set up a fresh 64-bit Debian 13 (Trixie) or Raspberry Pi OS Pi Zero 2 W. Substitute `YOUR_USER`, your repository URL, and Navidrome values everywhere they appear.

### 1. Flash and prepare the Pi

In Raspberry Pi Imager, set a username and password, configure the Wi-Fi country, and enable SSH. After first boot:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y \
  python3 python3-venv python3-pip \
  mpv network-manager bluez \
  pipewire pipewire-alsa pipewire-pulse wireplumber libspa-0.2-bluetooth
```

Confirm the basic platform services are available:

```bash
nmcli device status
wpctl status -n
bluetoothctl show
```

### 2. Install the application

```bash
cd /home/YOUR_USER
git clone <your-repository-url> pimusic
cd pimusic

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install fastapi "uvicorn[standard]" httpx
```

Create the private Navidrome configuration:

```bash
mkdir -p ~/.config/pimusic
chmod 700 ~/.config/pimusic
nano ~/.config/pimusic/pimusic.env
chmod 600 ~/.config/pimusic/pimusic.env
```

```ini
NAVIDROME_BASE=http://navidrome.local:4533
NAVIDROME_USER=your_navidrome_user
NAVIDROME_SALT=your_random_salt
NAVIDROME_TOKEN=the_matching_token_hash
```

### 3. Create user services

Enable user-service lingering once:

```bash
sudo loginctl enable-linger YOUR_USER
mkdir -p ~/.config/systemd/user/pimusic-web.service.d
```

Create `~/.config/systemd/user/pimusic-mpv.service`:

```ini
[Unit]
Description=PiMusic MPV Player
After=pipewire.service wireplumber.service

[Service]
ExecStart=/usr/bin/mpv --no-video --idle=yes --input-ipc-server=/tmp/pimusic-mpv.sock
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
```

Create `~/.config/systemd/user/pimusic-web.service`:

```ini
[Unit]
Description=PiMusic Web Backend
After=network-online.target pimusic-mpv.service
Wants=network-online.target

[Service]
WorkingDirectory=/home/YOUR_USER/pimusic
ExecStart=/home/YOUR_USER/pimusic/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
```

Create `~/.config/systemd/user/pimusic-web.service.d/override.conf`:

```ini
[Service]
EnvironmentFile=%h/.config/pimusic/pimusic.env
```

Replace `YOUR_USER` in the service file, then enable both services:

```bash
systemctl --user daemon-reload
systemctl --user enable --now pimusic-mpv.service pimusic-web.service
systemctl --user status pimusic-mpv.service pimusic-web.service --no-pager
```

Open `http://<pi-hostname-or-ip>:8000`. The **About / System Status** card shows the current Wi-Fi IP and MPV availability.

### 4. Connect audio and use PiMusic

Use the Bluetooth card to scan, pair, trust, connect, and select a speaker. Verify a selected audio output with:

```bash
wpctl status -n
bluetoothctl devices Connected
```

Click a song to replace the queue. Album and playlist pages offer **Play**, **Append to Queue**, and **Mix into Queue**. **Shuffle Upcoming** preserves the current track and randomizes only tracks that follow it.

### 5. Optional fallback hotspot

Install the fallback service if the Pi should offer its own Wi-Fi network whenever normal Wi-Fi is unavailable:

```bash
chmod +x system/install-hotspot-fallback.sh
sudo ./system/install-hotspot-fallback.sh
sudo systemctl start pimusic-hotspot-fallback.service
```

Join the configured `pimusic-setup` hotspot and browse to `http://10.42.0.1:8000`. When submitting normal Wi-Fi credentials, PiMusic must leave the setup hotspot, so the browser may lose its connection before it receives a response. Rejoin the normal network and open PiMusic at its hostname or current DHCP address. See [system/README.md](system/README.md) for testing, removal, and preferred-profile configuration.

## Architecture / Why These Settings Matter

### Headless appliance mode

The Pi Zero 2 W is resource-constrained. The tested platform runs headlessly without a display manager. If a desktop was installed but is not needed:

```bash
sudo systemctl set-default multi-user.target
sudo systemctl disable --now lightdm
```

Keep SSH enabled for administration and use a stable hostname or DHCP reservation.

### Linger and systemd user services

MPV, PipeWire, and PiMusic run in the same user session. `loginctl enable-linger` starts that user manager at boot and keeps it alive without an interactive login. Without it, the user services may not survive or start reliably on a headless appliance.

### Persistent MPV process

MPV runs with `--idle=yes` and exposes `/tmp/pimusic-mpv.sock`. The FastAPI backend sends JSON IPC commands to that one process instead of launching a player per track. This preserves the live queue across a web-service restart and makes playback controls responsive.

PiMusic separately saves queue metadata, playback recovery state, shuffle state, and volume under `~/.config/pimusic/`. If MPV later starts empty, PiMusic restores the saved queue position and paused state.

### PipeWire and Bluetooth

PiMusic uses `bluetoothctl` for Bluetooth management and `wpctl` for PipeWire sink selection. Do not depend on `pactl`. Bluetooth outputs appear in PipeWire as `bluez_output.<MAC-with-underscores>.*`, which PiMusic uses to select the appropriate sink.

RTKit warnings from PipeWire can be non-fatal. Treat them as a problem only when audio is also failing.

PiMusic saves the preferred Bluetooth MAC through `/usr/local/bin/pimusic-set-bluetooth-device`, which should validate the address and write `/etc/pimusic/bluetooth.conf`. Give the web-service user access only to that exact root helper:

```sudoers
YOUR_USER ALL=(root) NOPASSWD: /usr/local/bin/pimusic-set-bluetooth-device
```

For automatic speaker reconnect after boot, install a root-owned helper that reads this file, waits for `bluetoothctl show` to report `Powered: yes`, and retries `bluetoothctl connect`. Run it from a oneshot `pimusic-bluetooth.service` ordered after `bluetooth.service` and `network-online.target`.

### Wi-Fi power saving

The Pi Zero 2 W shares its Wi-Fi and Bluetooth radio. If Bluetooth becomes unreliable, first check power with `vcgencmd get_throttled`. Then consider disabling Wi-Fi power saving for the active NetworkManager profile:

```bash
nmcli -g GENERAL.CONNECTION device show wlan0
sudo nmcli connection modify "your-active-profile" 802-11-wireless.powersave 2
sudo nmcli connection up "your-active-profile"
iw dev wlan0 get power_save
```

The expected output is `Power save: off`. Discover the active profile; do not hardcode a development profile name.

### Network and security

Navidrome credentials stay in the private environment file and are never sent to the browser. Stream URLs and cover art are handled by the PiMusic backend. PiMusic is intended for a trusted home network; use a reverse proxy with authentication before exposing it externally.

## Updating and troubleshooting

Validate Python, then restart the backend after an `app.py` change:

```bash
cd ~/pimusic
.venv/bin/python -m py_compile app.py
systemctl --user restart pimusic-web.service
```

Hard-refresh the browser after changes under `static/`. Check service health with:

```bash
systemctl --user status pimusic-web.service --no-pager -n 80
systemctl --user show pimusic-web.service -p ActiveState -p SubState -p NRestarts -p ExecMainStatus
```

If startup errors are hidden, stop the service and run it directly:

```bash
systemctl --user stop pimusic-web.service
cd ~/pimusic
set -a; source ~/.config/pimusic/pimusic.env; set +a
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Useful network checks:

```bash
getent hosts <navidrome-host>
curl -I https://<navidrome-host>
```

## Project layout

```text
app.py                  FastAPI server, Navidrome integration, MPV control
static/index.html       Application markup
static/app.js           Browser behavior and API calls
static/style.css        Responsive interface styles
static/pimusic-icon.png Browser and application icon
system/                 Optional hotspot fallback service and installer
```
