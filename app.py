import json
import socket
import httpx
import hashlib
import subprocess
import time
import os
import re
import tempfile
import random
import secrets
from threading import RLock

from urllib.parse import urlparse, parse_qs
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel


NAVIDROME_BASE = os.environ["NAVIDROME_BASE"]
NAVIDROME_USER = os.environ["NAVIDROME_USER"]
NAVIDROME_SALT = os.environ["NAVIDROME_SALT"]
NAVIDROME_TOKEN = os.environ["NAVIDROME_TOKEN"]

app = FastAPI(title="PiMusic")

PIMUSIC_STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pimusic"
PIMUSIC_ENV_FILE = PIMUSIC_STATE_DIR / "pimusic.env"
QUEUE_METADATA_FILE = PIMUSIC_STATE_DIR / "queue.json"
PLAYBACK_STATE_FILE = QUEUE_METADATA_FILE.with_name("playback.json")
VOLUME_STATE_FILE = QUEUE_METADATA_FILE.with_name("volume.json")
OUTPUT_STATE_FILE = QUEUE_METADATA_FILE.with_name("browser-playback.json")
queue_metadata = {}  # Legacy metadata, used only while migrating older installs.
queue_state = {
    "version": 2,
    "items": [],
    "current_index": -1,
    "paused": True,
    "position": 0.0,
    "shuffle": False,
}
queue_state_from_v2 = False
last_persisted_queue_progress = None
browser_playback = {
    "mode": "pi",
    "lease": None,
    "lease_expires": 0,
    "paused": True,
    "position": 0.0,
    "revision": 0,
}

class VolumeRequest(BaseModel):
    volume: float

class SeekRequest(BaseModel):
    position: float

class QueueMoveRequest(BaseModel):
    target_position: int
    queue_ids: list[str]

class BluetoothDeviceRequest(BaseModel):
    address: str

class WifiConnectRequest(BaseModel):
    ssid: str
    password: str | None = None

class NavidromeConfigRequest(BaseModel):
    base: str
    user: str
    password: str

class BrowserProgressRequest(BaseModel):
    lease: str
    position: float = 0
    paused: bool = True

class BrowserLeaseRequest(BaseModel):
    lease: str

class BrowserEndedRequest(BaseModel):
    lease: str

MPV_SOCKET = "/tmp/pimusic-mpv.sock"
HOTSPOT_CONTROL = "/usr/local/bin/pimusic-hotspot-control"
mpv_lock = RLock()
playback_lock = RLock()
queue_lock = RLock()
navidrome_lock = RLock()
bluetooth_scan_lock = RLock()
PIMUSIC_VERSION = "1.0"
# Background mobile tabs may suspend JavaScript timers while their audio is
# legitimately still playing. Keep a bounded safety window rather than
# treating a brief tab switch as a disconnected listener.
BROWSER_LEASE_SECONDS = 5 * 60

def split_nmcli_fields(line: str):
    fields = []
    field = []
    escaped = False
    for character in line:
        if escaped:
            field.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(character)
    fields.append("".join(field))
    return fields

def wifi_status_info():
    result = subprocess.run(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="NetworkManager is unavailable")

    wifi = next(
        (split_nmcli_fields(line) for line in result.stdout.splitlines()
         if len(split_nmcli_fields(line)) >= 4 and split_nmcli_fields(line)[1] == "wifi"),
        None,
    )
    if not wifi:
        return {"available": False, "connected": False}

    device, _, state, connection = wifi[:4]
    ip_result = subprocess.run(
        ["nmcli", "-g", "IP4.ADDRESS", "device", "show", device],
        capture_output=True,
        text=True,
        timeout=10,
    )
    ip_address = next(
        (address.split("/", 1)[0] for address in ip_result.stdout.splitlines() if address),
        None,
    )
    return {
        "available": True,
        "device": device,
        "state": state,
        "connection": None if connection == "--" else connection,
        "ip_address": ip_address,
        "connected": state == "connected",
    }

def wifi_networks():
    status = wifi_status_info()
    if not status["available"]:
        return {"available": False, "networks": []}

    result = subprocess.run(
        [
            "nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
            "device", "wifi", "list", "ifname", status["device"], "--rescan", "yes",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="Unable to scan Wi-Fi networks")

    networks = {}
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) < 4 or not fields[1]:
            continue
        active, ssid, signal, security = fields[:4]
        network = {
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else 0,
            "security": "Open" if security in ("", "--") else security,
            "active": active == "*",
        }
        existing = networks.get(ssid)
        if (
            not existing
            or (network["active"] and not existing["active"])
            or (not existing["active"] and network["signal"] > existing["signal"])
        ):
            networks[ssid] = network

    return {
        "available": True,
        "networks": sorted(networks.values(), key=lambda network: (not network["active"], -network["signal"], network["ssid"].lower())),
    }

def saved_wifi_connections():
    result = subprocess.run(
        ["nmcli", "-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=503, detail="Unable to read saved Wi-Fi networks")

    connections = []
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) < 3 or fields[1] not in ("wifi", "802-11-wireless"):
            continue
        connections.append({
            "name": fields[0],
            "autoconnect": fields[2] == "yes",
        })
    return {"connections": connections}

def wifi_command_error(result, fallback):
    """Return an actionable, credential-safe summary of an nmcli failure."""
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
    if any(marker in output for marker in ("secrets were required", "no secrets", "authentication", "wrong password")):
        return "Wi-Fi authentication failed. Check the password and try again."
    if any(marker in output for marker in ("not authorized", "permission denied")):
        return "PiMusic is not authorized to manage Wi-Fi connections."
    if any(marker in output for marker in ("unavailable", "unmanaged", "not found", "no network with ssid")):
        return "That Wi-Fi network or adapter is not currently available."
    if any(marker in output for marker in ("timed out", "timeout")):
        return "Timed out while joining the Wi-Fi network. Try again from closer range."
    return fallback

def connect_wifi(request: WifiConnectRequest):
    status = wifi_status_info()
    ssid = request.ssid.strip()
    if not status["available"]:
        raise HTTPException(status_code=503, detail="No Wi-Fi adapter is available")
    if not ssid or len(ssid) > 32:
        raise HTTPException(status_code=400, detail="Enter a valid Wi-Fi network name")

    security = "secured" if request.password else "open"
    profile = f"PiMusic Wi-Fi {security}: {ssid}"
    profile_exists = subprocess.run(
        ["nmcli", "connection", "show", "id", profile],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if profile_exists.returncode != 0:
        created = subprocess.run(
            [
                "nmcli", "connection", "add", "type", "wifi", "ifname", status["device"],
                "con-name", profile, "ssid", ssid,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if created.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=wifi_command_error(created, "Unable to prepare that Wi-Fi connection"),
            )

    if request.password:
        secured = subprocess.run(
            ["nmcli", "connection", "modify", profile, "wifi-sec.key-mgmt", "wpa-psk"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if secured.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=wifi_command_error(secured, "Unable to prepare that Wi-Fi connection"),
            )

    setup_paused = False
    setup_status = hotspot_setup_status()
    if setup_status.get("state") == "PROVISIONING":
        hotspot_setup_status("prepare-connect")
        setup_paused = True

    password_file = None
    try:
        command = ["nmcli", "--wait", "30", "connection", "up", "id", profile, "ifname", status["device"]]
        if request.password:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, dir=tempfile.gettempdir()) as file:
                file.write(f"802-11-wireless-security.psk:{request.password}\n")
                password_file = file.name
            os.chmod(password_file, 0o600)
            command.extend(["passwd-file", password_file])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=40,
        )
    except subprocess.TimeoutExpired as error:
        if setup_paused:
            hotspot_setup_status("stop")
        raise HTTPException(
            status_code=504,
            detail="Timed out while joining the Wi-Fi network. Try again from closer range.",
        ) from error
    except OSError as error:
        if setup_paused:
            hotspot_setup_status("stop")
        raise HTTPException(status_code=503, detail="Unable to activate the Wi-Fi connection") from error
    finally:
        if password_file:
            Path(password_file).unlink(missing_ok=True)
    if result.returncode != 0:
        if setup_paused:
            hotspot_setup_status("stop")
        raise HTTPException(
            status_code=400,
            detail=wifi_command_error(result, "Unable to connect to that Wi-Fi network"),
        )
    connection_status = wifi_status_info()
    if not connection_status["connected"]:
        if setup_paused:
            hotspot_setup_status("stop")
        raise HTTPException(status_code=400, detail="Wi-Fi connection did not become active")

    # Resume normal fallback only after NetworkManager has activated normal
    # Wi-Fi, so it cannot race the requested connection attempt.
    if setup_paused:
        hotspot_setup_status("stop")
    return {
        "wifi": connection_status,
        "handoff": setup_paused,
        "message": "PiMusic joined the requested Wi-Fi network.",
    }

def hotspot_setup_status(action="status"):
    if not Path(HOTSPOT_CONTROL).is_file():
        return {
            "installed": False,
            "service_active": False,
            "forced": False,
            "hotspot_active": False,
            "state": "UNAVAILABLE",
            "reason": "not_installed",
        }

    result = subprocess.run(
        ["sudo", "-n", HOTSPOT_CONTROL, action],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=503,
            detail="Setup hotspot control is unavailable. Check its installation and sudoers access.",
        )
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise HTTPException(status_code=503, detail="Setup hotspot control returned an invalid status") from error

def switch_bluetooth_output(address: str):
    mac_fragment = address.replace(":", "_").upper()
    sink_id = None

    # Wait for PipeWire to create the Bluetooth sink.
    for _ in range(20):
        result = subprocess.run(
            ["wpctl", "status", "-n"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        for line in result.stdout.splitlines():
            if f"bluez_output.{mac_fragment}" not in line:
                continue

            # Example:
            # │  *   78. bluez_output.08_EB_ED_E9_86_5D.1 [vol: 0.62]
            cleaned = (
                line.replace("│", " ")
                    .replace("*", " ")
                    .strip()
            )

            parts = cleaned.split()

            if parts and parts[0].endswith("."):
                sink_id = parts[0].rstrip(".")
                break

        if sink_id:
            break

        time.sleep(0.5)

    if not sink_id:
        raise RuntimeError(
            f"Bluetooth audio sink did not appear for {address}"
        )

    # Make the selected Bluetooth device the default output.
    result = subprocess.run(
        ["wpctl", "set-default", sink_id],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or f"Unable to set PipeWire sink {sink_id} as default"
        )

    # Disconnect other audio devices, preserving unrelated Bluetooth connections.
    connected = subprocess.run(
        ["bluetoothctl", "devices", "Connected"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    for line in connected.stdout.splitlines():
        parts = line.split(" ", 2)

        if len(parts) < 2 or parts[0] != "Device":
            continue

        other_address = parts[1]

        if other_address.lower() == address.lower():
            continue

        if not bluetooth_info(other_address)["audio"]:
            continue

        subprocess.run(
            ["bluetoothctl", "disconnect", other_address],
            capture_output=True,
            text=True,
            timeout=10,
        )

def bluetooth_failure_message(result, fallback):
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
    if "connection refused" in output:
        return "The speaker refused the Bluetooth connection. Check that it is powered on and not connected to another device."
    if "inprogress" in output or "in progress" in output:
        return "Bluetooth is already scanning. Wait for the current scan to finish and try again."
    if "not available" in output or "no default controller" in output:
        return "Bluetooth is not available on the Pi."
    return fallback

def bluetooth_info(address: str):
    result = subprocess.run(
        ["bluetoothctl", "info", address],
        capture_output=True,
        text=True,
        timeout=10,
    )

    info = {
        "address": address,
        "name": None,
        "connected": False,
        "paired": False,
        "trusted": False,
        "audio": False,
    }

    for line in result.stdout.splitlines():
        line = line.strip()

        if line.startswith("Name:"):
            info["name"] = line.split(":", 1)[1].strip()

        elif line.startswith("Connected:"):
            info["connected"] = line.endswith("yes")

        elif line.startswith("Paired:"):
            info["paired"] = line.endswith("yes")

        elif line.startswith("Trusted:"):
            info["trusted"] = line.endswith("yes")

        elif line.startswith("UUID:"):
            # A2DP Sink, HSP Headset, HFP Hands-Free.
            if re.search(r"0000(?:110b|1108|111e)-0000-1000-8000-00805f9b34fb", line, re.I):
                info["audio"] = True

        elif line.startswith("Icon:"):
            if line.split(":", 1)[1].strip() in ("audio-headset", "audio-headphones", "audio-card"):
                info["audio"] = True

        elif line.startswith("Class:"):
            try:
                device_class = int(line.split(":", 1)[1].strip(), 16)
                # Audio/video major class with an output-capable minor class.
                major = (device_class >> 8) & 0x1f
                minor = (device_class >> 2) & 0x3f
                if major == 4 and minor in (1, 2, 4, 5, 6, 7, 8, 10):
                    info["audio"] = True
            except ValueError:
                pass

    return info

def save_volume_state(volume):
    VOLUME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = VOLUME_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"volume": volume}), encoding="utf-8")
    temporary.replace(VOLUME_STATE_FILE)

def restore_volume_state():
    try:
        volume = json.loads(VOLUME_STATE_FILE.read_text(encoding="utf-8")).get("volume")
        if isinstance(volume, (int, float)):
            mpv_command(["set_property", "volume", max(0, min(100, volume))])
    except (OSError, ValueError, TypeError, RuntimeError):
        pass

def navidrome_auth_params():
    return {
        "v": "1.16.1",
        "c": "pimusic",
        "u": NAVIDROME_USER,
        "s": NAVIDROME_SALT,
        "t": NAVIDROME_TOKEN,
    }

def navidrome_params():
    params = navidrome_auth_params()
    params["f"] = "json"
    return params

def normalized_navidrome_base(value: str):
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="Enter a complete http:// or https:// Navidrome server address")
    return candidate

def navidrome_config_params(user: str, salt: str, token: str):
    return {
        "v": "1.16.1",
        "c": "pimusic",
        "u": user,
        "s": salt,
        "t": token,
        "f": "json",
    }

def navidrome_token(password: str, salt: str):
    """Create the Subsonic token without retaining the Navidrome password."""
    return hashlib.md5(f"{password}{salt}".encode("utf-8")).hexdigest()

def validate_navidrome_config(base: str, user: str, salt: str, token: str):
    try:
        response = httpx.get(
            f"{base}/rest/getPlaylists.view",
            params=navidrome_config_params(user, salt, token),
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json().get("subsonic-response", {})
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Unable to reach or validate that Navidrome server") from error
    if payload.get("status") != "ok":
        raise HTTPException(status_code=400, detail="Navidrome rejected those credentials")

def write_navidrome_config(base: str, user: str, salt: str, token: str):
    def environment_line(name: str, value: str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        return f'{name}="{escaped}"\n'

    PIMUSIC_STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PIMUSIC_STATE_DIR, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=PIMUSIC_STATE_DIR,
        prefix=".pimusic.env.",
    ) as file:
        file.write(environment_line("NAVIDROME_BASE", base))
        file.write(environment_line("NAVIDROME_USER", user))
        file.write(environment_line("NAVIDROME_SALT", salt))
        file.write(environment_line("NAVIDROME_TOKEN", token))
        temporary = Path(file.name)
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(PIMUSIC_ENV_FILE)
    finally:
        temporary.unlink(missing_ok=True)

def reset_playback_for_library_change():
    global queue_metadata
    with mpv_lock:
        mpv_command(["set_property", "pause", True])
        clear_mpv_playlist()
    queue_metadata = {}
    with queue_lock:
        queue_state.update({"items": [], "current_index": -1, "paused": True, "position": 0.0, "shuffle": False})
    browser_playback.update({"mode": "pi", "lease": None, "lease_expires": 0, "paused": True, "position": 0.0})
    save_queue_state()
    browser_state_changed()

def navidrome_song(song_id: str):
    params = navidrome_params()
    params["id"] = song_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getSong.view",
        params=params,
        timeout=10.0,
    )

    response.raise_for_status()

    return response.json()["subsonic-response"]["song"]

def mpv_command(command):
    request = json.dumps({
        "command": command,
        "request_id": 1
    }) + "\n"

    with mpv_lock:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(MPV_SOCKET)
            sock.sendall(request.encode())

            buffer = b""

            while True:
                chunk = sock.recv(4096)

                if not chunk:
                    raise RuntimeError("MPV IPC connection closed")

                buffer += chunk

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)

                    if not line:
                        continue

                    message = json.loads(line.decode())

                    if message.get("request_id") == 1:
                        return message

def mpv_property(name):
    result = mpv_command(["get_property", name])

    if result.get("error") != "success":
        return None

    return result.get("data")

def mpv_metadata():
    result = mpv_command(["get_property", "metadata"])

    if result.get("error") != "success":
        return {}

    return result.get("data") or {}

def navidrome_stream_url(song_id: str):
    params = navidrome_auth_params()
    params["id"] = song_id

    return str(
        httpx.URL(
            f"{NAVIDROME_BASE}/rest/stream.view",
            params=params,
        )
    )

def save_browser_playback():
    """Persist only Pi-owned browser-session state; the lease itself is never reused."""
    OUTPUT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "mode": browser_playback["mode"],
        "paused": browser_playback["paused"],
        "position": browser_playback["position"],
        "revision": browser_playback["revision"],
    }
    temporary = OUTPUT_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(OUTPUT_STATE_FILE)

def load_browser_playback():
    """A browser cannot survive a backend restart, so always return to paused Pi mode."""
    was_browser = False
    try:
        state = json.loads(OUTPUT_STATE_FILE.read_text(encoding="utf-8"))
        browser_playback["position"] = max(0, float(state.get("position", 0)))
        browser_playback["revision"] = int(state.get("revision", 0))
        was_browser = state.get("mode") == "browser"
    except (OSError, ValueError, TypeError):
        pass
    browser_playback.update({
        "mode": "pi",
        "lease": None,
        "lease_expires": 0,
        "paused": True,
    })
    if was_browser:
        with queue_lock:
            queue_state.update({"position": browser_playback["position"], "paused": True})
        save_queue_state()

def browser_lease_active(lease=None):
    if browser_playback["mode"] != "browser":
        return False
    if time.monotonic() >= browser_playback["lease_expires"]:
        browser_playback.update({"mode": "pi", "lease": None, "lease_expires": 0, "paused": True})
        with queue_lock:
            queue_state["paused"] = True
        save_queue_state()
        mpv_command(["set_property", "pause", True])
        save_browser_playback()
        return False
    return lease is None or secrets.compare_digest(lease, browser_playback["lease"] or "")

def browser_state_changed():
    browser_playback["revision"] += 1
    save_browser_playback()

def pause_mpv_for_browser():
    result = mpv_command(["set_property", "pause", True])
    if result.get("error") != "success":
        raise HTTPException(status_code=503, detail="Unable to pause Pi audio for browser playback")

def browser_stream_response(song_id: str, request: Request):
    lease = request.query_params.get("lease", "")
    if not browser_lease_active(lease):
        raise HTTPException(status_code=403, detail="Browser playback session is no longer active")

    headers = {}
    if request.headers.get("range"):
        headers["range"] = request.headers["range"]
    client = httpx.Client(timeout=httpx.Timeout(30.0, read=60.0))
    upstream = client.send(
        client.build_request("GET", navidrome_stream_url(song_id), headers=headers),
        stream=True,
    )
    if upstream.status_code >= 400:
        upstream.close()
        client.close()
        raise HTTPException(status_code=502, detail="Unable to stream this track from Navidrome")

    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name.lower() in {"accept-ranges", "content-length", "content-range"}
    }
    media_type = upstream.headers.get("content-type", "audio/mpeg")

    def body():
        try:
            yield from upstream.iter_bytes()
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=media_type,
        headers=response_headers,
        background=BackgroundTask(lambda: (upstream.close(), client.close())),
    )

def clear_mpv_playlist():
    playlist = mpv_property("playlist")

    if playlist is None:
        raise HTTPException(
            status_code=500,
            detail="Unable to read the existing queue",
        )

    for position in range(len(playlist) - 1, -1, -1):
        result = mpv_command(["playlist-remove", position])

        if result.get("error") != "success":
            raise HTTPException(
                status_code=500,
                detail="Unable to clear the existing queue",
            )

def queue_item_from_song(song):
    """Make a durable queue entry. Queue IDs identify an occurrence, not a song."""
    song_id = str(song["id"])
    cover_art = song.get("coverArt")
    if not cover_art:
        try:
            cover_art = navidrome_song(song_id).get("coverArt")
        except Exception:
            cover_art = None
    return {
        "queue_id": secrets.token_urlsafe(12),
        "song_id": song_id,
        "title": song.get("title"),
        "artist": song.get("artist"),
        "album": song.get("album"),
        "coverArt": cover_art,
        "duration": song.get("duration") or 0,
    }


def save_queue_state():
    """Atomically persist the queue; it is the playback authority, not MPV."""
    global last_persisted_queue_progress
    with queue_lock:
        state = {
            "version": 2,
            "items": queue_state["items"],
            "current_index": queue_state["current_index"],
            "paused": queue_state["paused"],
            "position": queue_state["position"],
            "shuffle": queue_state["shuffle"],
        }
    QUEUE_METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUEUE_METADATA_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(QUEUE_METADATA_FILE)
    last_persisted_queue_progress = (
        state["current_index"], state["paused"], state["position"],
    )


def load_queue_state():
    """Load v2 state, or migrate the old metadata/playback pair once."""
    global queue_metadata, queue_state, queue_state_from_v2
    queue_metadata = {}
    try:
        saved = json.loads(QUEUE_METADATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        saved = {}

    if isinstance(saved, dict) and saved.get("version") == 2 and isinstance(saved.get("items"), list):
        queue_state_from_v2 = True
        items = [item for item in saved["items"] if isinstance(item, dict) and item.get("queue_id") and item.get("song_id")]
        current = saved.get("current_index", -1)
        queue_state = {
            "version": 2,
            "items": items,
            "current_index": current if isinstance(current, int) and 0 <= current < len(items) else (-1 if not items else 0),
            "paused": bool(saved.get("paused", True)),
            "position": max(0, float(saved.get("position", 0) or 0)),
            "shuffle": bool(saved.get("shuffle", False)),
        }
        return

    # Previous queue.json was song-ID metadata; playback.json supplied the order.
    queue_state_from_v2 = False
    queue_metadata = saved if isinstance(saved, dict) else {}
    try:
        legacy_playback = json.loads(PLAYBACK_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        legacy_playback = {}
    items = []
    for song_id in legacy_playback.get("song_ids", []):
        metadata = queue_metadata.get(str(song_id), {})
        items.append({
            "queue_id": secrets.token_urlsafe(12), "song_id": str(song_id),
            "title": metadata.get("title"), "artist": metadata.get("artist"),
            "album": metadata.get("album"), "coverArt": metadata.get("coverArt"),
            "duration": metadata.get("duration") or 0,
        })
    current = legacy_playback.get("position", -1)
    queue_state = {
        "version": 2, "items": items,
        "current_index": current if isinstance(current, int) and 0 <= current < len(items) else (-1 if not items else 0),
        "paused": bool(legacy_playback.get("paused", True)), "position": 0.0, "shuffle": False,
    }
    if items:
        save_queue_state()


def queue_snapshot():
    with queue_lock:
        return {
            "items": [dict(item) for item in queue_state["items"]],
            "current_index": queue_state["current_index"],
            "paused": queue_state["paused"], "position": queue_state["position"],
            "shuffle": queue_state["shuffle"],
        }


def render_queue_to_mpv():
    """Make MPV render the authoritative queue, preserving its playback state."""
    state = queue_snapshot()
    with mpv_lock:
        # Do not let a replacement playlist briefly play from its beginning
        # while its stream is still loading and before its saved seek applies.
        mpv_command(["set_property", "pause", True])
        clear_mpv_playlist()
        for index, item in enumerate(state["items"]):
            result = mpv_command(["loadfile", navidrome_stream_url(item["song_id"]), "replace" if index == 0 else "append"])
            if result.get("error") != "success":
                raise HTTPException(status_code=500, detail="Unable to render the queue on the Pi")
        if state["current_index"] >= 0:
            mpv_command(["set_property", "playlist-pos", state["current_index"]])
            target_position = state["position"]
            if target_position > 0:
                # Network streams load asynchronously. A successful IPC reply
                # alone does not mean MPV has a seekable demuxer yet.
                for _ in range(30):
                    result = mpv_command(["set_property", "time-pos", target_position])
                    if result.get("error") == "success":
                        actual_position = mpv_property("time-pos")
                        if isinstance(actual_position, (int, float)) and abs(actual_position - target_position) < 1:
                            break
                    time.sleep(0.1)
        # Browser output always leaves the Pi renderer silent.
        mpv_command(["set_property", "pause", True if browser_playback["mode"] == "browser" else state["paused"]])


def mpv_queue_song_ids():
    playlist = mpv_property("playlist")
    if playlist is None:
        return None
    song_ids = []
    for entry in playlist:
        try:
            song_id = parse_qs(urlparse(entry.get("filename", "")).query).get("id", [None])[0]
        except Exception:
            song_id = None
        if song_id is None:
            return None
        song_ids.append(str(song_id))
    return song_ids


def reorder_mpv_renderer(previous_items, updated_items):
    """Mirror a backend reorder without replacing the active MPV stream."""
    previous_ids = [item["queue_id"] for item in previous_items]
    updated_ids = [item["queue_id"] for item in updated_items]
    if set(previous_ids) != set(updated_ids) or len(previous_ids) != len(updated_ids):
        return False
    if mpv_queue_song_ids() != [item["song_id"] for item in previous_items]:
        return False

    renderer_order = previous_ids[:]
    with mpv_lock:
        for target, queue_id in enumerate(updated_ids):
            source = renderer_order.index(queue_id)
            if source == target:
                continue
            # MPV inserts before the target in the original playlist.
            insertion = target + 1 if source < target else target
            result = mpv_command(["playlist-move", source, insertion])
            if result.get("error") != "success":
                return False
            renderer_order.insert(target, renderer_order.pop(source))
    return True


def append_to_mpv_renderer(previous_items, additions):
    """Append new backend entries without replacing the active MPV stream."""
    if mpv_queue_song_ids() != [item["song_id"] for item in previous_items]:
        return False
    with mpv_lock:
        for item in additions:
            result = mpv_command(["loadfile", navidrome_stream_url(item["song_id"]), "append"])
            if result.get("error") != "success":
                return False
    return True


def sync_queue_from_mpv():
    """Record Pi-renderer progress without ever taking queue order from MPV."""
    if browser_playback["mode"] == "browser":
        return
    state = queue_snapshot()
    if not state["items"]:
        return
    index = mpv_property("playlist-pos")
    if not isinstance(index, int) or not 0 <= index < len(state["items"]):
        return
    position = max(0, float(mpv_property("time-pos") or 0))
    paused = mpv_property("pause") is True or mpv_property("idle-active") is True
    with queue_lock:
        persisted = last_persisted_queue_progress
        should_persist = (
            persisted is None or persisted[0] != index or persisted[1] != paused
            or abs(persisted[2] - position) >= 5
        )
        queue_state.update({"current_index": index, "position": position, "paused": paused})
    if should_persist:
        save_queue_state()


def set_queue_from_songs(songs):
    if not songs:
        raise HTTPException(status_code=400, detail="Queue contains no songs")
    browser_active = browser_lease_active()
    with queue_lock:
        queue_state.update({
            "items": [queue_item_from_song(song) for song in songs], "current_index": 0,
            "paused": False, "position": 0.0, "shuffle": False,
        })
    save_queue_state()
    render_queue_to_mpv()
    if browser_active:
        browser_playback.update({"position": 0.0, "paused": False})
        browser_state_changed()
    return {"status": "playing", "queue_length": len(songs)}


def play_song_queue(songs):
    return set_queue_from_songs(songs)


def add_songs_to_queue(songs, mix=False):
    if not songs:
        raise HTTPException(status_code=400, detail="Collection contains no songs")
    state = queue_snapshot()
    if not state["items"] or state["current_index"] < 0:
        return set_queue_from_songs(songs)
    additions = [queue_item_from_song(song) for song in songs]
    with queue_lock:
        if mix:
            for item in additions:
                target = random.randint(queue_state["current_index"] + 1, len(queue_state["items"]))
                queue_state["items"].insert(target, item)
        else:
            queue_state["items"].extend(additions)
        queue_state["shuffle"] = False
    save_queue_state()
    updated = queue_snapshot()["items"]
    if not append_to_mpv_renderer(state["items"], additions):
        render_queue_to_mpv()
    elif mix and not reorder_mpv_renderer(state["items"] + additions, updated):
        render_queue_to_mpv()
    return {"status": "mixed" if mix else "appended", "queue_length": len(additions)}


# Runs at import so endpoint handlers always have a queue model available.
load_queue_state()

@app.on_event("startup")
def restore_playback_queue():
    load_browser_playback()
    restore_volume_state()
    try:
        if queue_snapshot()["items"]:
            render_queue_to_mpv()
            return
        # An upgrade can find a still-running legacy MPV queue before the old
        # recovery file was written. Import it once, then persist v2 state.
        # Never import a stale MPV playlist over an intentional empty v2 queue.
        if queue_state_from_v2:
            return
        playlist = mpv_property("playlist") or []
        if playlist:
            current_index = mpv_property("playlist-pos")
            current_index = current_index if isinstance(current_index, int) else 0
            position = max(0, float(mpv_property("time-pos") or 0))
            paused = mpv_property("pause") is True or mpv_property("idle-active") is True
            songs = []
            for entry in playlist:
                song_id = parse_qs(urlparse(entry.get("filename", "")).query).get("id", [None])[0]
                if song_id:
                    songs.append(navidrome_song(song_id))
            if songs:
                with queue_lock:
                    queue_state.update({
                        "items": [queue_item_from_song(song) for song in songs],
                        "current_index": min(max(0, current_index), len(songs) - 1),
                        "position": position, "paused": paused, "shuffle": False,
                    })
                save_queue_state()
                render_queue_to_mpv()
    except Exception:
        pass

@app.get("/api/cover/{cover_art_id}")
def get_cover_art(cover_art_id: str):
    params = navidrome_auth_params()
    params["id"] = cover_art_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getCoverArt.view",
        params=params,
        timeout=20.0,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail="Failed to fetch cover art",
        )

    return Response(
        content=response.content,
        media_type=response.headers.get("content-type", "image/jpeg"),
    )

@app.get("/api/navidrome/config")
def navidrome_config_status():
    return {"base": NAVIDROME_BASE}

@app.post("/api/navidrome/config")
def set_navidrome_config(request: NavidromeConfigRequest):
    global NAVIDROME_BASE, NAVIDROME_USER, NAVIDROME_SALT, NAVIDROME_TOKEN
    base = normalized_navidrome_base(request.base)
    values = [request.user.strip(), request.password]
    if (
        any(not value or len(value) > 512 or any(character in value for character in "\r\n") for value in values)
        or any(character in base for character in "\r\n")
    ):
        raise HTTPException(status_code=400, detail="Enter valid Navidrome credentials")
    user, password = values
    salt = secrets.token_hex(16)
    token = navidrome_token(password, salt)

    with navidrome_lock:
        validate_navidrome_config(base, user, salt, token)
        reset_playback_for_library_change()
        write_navidrome_config(base, user, salt, token)
        NAVIDROME_BASE = base
        NAVIDROME_USER = user
        NAVIDROME_SALT = salt
        NAVIDROME_TOKEN = token

    return {"status": "configured", "base": NAVIDROME_BASE}

@app.post("/api/output/browser")
def use_browser_output():
    sync_queue_from_mpv()
    state = queue_snapshot()
    if not state["items"]:
        raise HTTPException(status_code=400, detail="Choose music before listening in this browser")
    position = state["position"]
    paused = state["paused"]
    lease = secrets.token_urlsafe(32)
    browser_playback.update({
        "mode": "browser",
        "lease": lease,
        "lease_expires": time.monotonic() + BROWSER_LEASE_SECONDS,
        "paused": paused,
        "position": position,
    })
    pause_mpv_for_browser()
    browser_state_changed()
    return {"mode": "browser", "lease": lease, "position": position, "paused": paused, "revision": browser_playback["revision"]}

@app.post("/api/output/pi")
def use_pi_output(request: BrowserLeaseRequest):
    if not browser_lease_active(request.lease):
        raise HTTPException(status_code=409, detail="This browser no longer owns the playback session")
    position = max(0, browser_playback["position"])
    paused = browser_playback["paused"]
    browser_playback.update({"mode": "pi", "lease": None, "lease_expires": 0})
    with queue_lock:
        queue_state.update({"position": position, "paused": paused})
    save_queue_state()
    render_queue_to_mpv()
    browser_state_changed()
    return {"mode": "pi", "position": position, "paused": paused}

@app.post("/api/browser/progress")
def browser_progress(request: BrowserProgressRequest):
    if not browser_lease_active(request.lease):
        raise HTTPException(status_code=409, detail="Browser playback session expired")
    browser_playback.update({
        "lease_expires": time.monotonic() + BROWSER_LEASE_SECONDS,
        "position": max(0, request.position),
        "paused": request.paused,
    })
    with queue_lock:
        persisted = last_persisted_queue_progress
        should_persist = (
            persisted is None or persisted[1] != request.paused
            or abs(persisted[2] - browser_playback["position"]) >= 5
        )
        queue_state.update({"position": browser_playback["position"], "paused": request.paused})
    if should_persist:
        save_queue_state()
    # Keep the dormant renderer at the same point for a fast handoff.
    mpv_command(["set_property", "time-pos", browser_playback["position"]])
    pause_mpv_for_browser()
    save_browser_playback()
    return {"revision": browser_playback["revision"]}

@app.post("/api/browser/ended")
def browser_ended(request: BrowserEndedRequest):
    if not browser_lease_active(request.lease):
        raise HTTPException(status_code=409, detail="Browser playback session expired")
    state = queue_snapshot()
    if state["current_index"] < 0:
        with queue_lock:
            queue_state.update({"paused": True, "position": 0.0})
        browser_playback.update({"position": 0.0, "paused": True})
        save_queue_state()
        browser_state_changed()
        return {"status": "ended", "revision": browser_playback["revision"]}
    if mpv_property("loop-file") == "inf":
        next_index = state["current_index"]
    elif state["current_index"] + 1 < len(state["items"]):
        next_index = state["current_index"] + 1
    elif mpv_property("loop-playlist") == "inf":
        next_index = 0
    else:
        with queue_lock:
            queue_state.update({"paused": True, "position": 0.0})
        browser_playback.update({"position": 0.0, "paused": True})
        save_queue_state()
        browser_state_changed()
        return {"status": "ended", "revision": browser_playback["revision"]}
    with queue_lock:
        queue_state.update({"current_index": next_index, "position": 0.0, "paused": False})
    save_queue_state()
    render_queue_to_mpv()
    browser_playback.update({"position": 0.0, "paused": False})
    browser_state_changed()
    return {"status": "advanced", "revision": browser_playback["revision"]}

@app.get("/api/browser/stream/{song_id}")
def browser_stream(song_id: str, request: Request):
    return browser_stream_response(song_id, request)

@app.post("/api/bluetooth/connect-device")
def bluetooth_connect_device(request: BluetoothDeviceRequest):
    address = request.address
    try:
        info = bluetooth_info(address)

        if not info["connected"]:
            result = subprocess.run(
                ["bluetoothctl", "connect", address],
                capture_output=True,
                text=True,
                timeout=20,
            )

            if result.returncode != 0 or "Connection successful" not in result.stdout:
                raise HTTPException(
                    status_code=503,
                    detail=bluetooth_failure_message(result, "Unable to connect to the Bluetooth speaker."),
                )

        switch_bluetooth_output(address)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=503, detail="Timed out while connecting to the Bluetooth speaker.") from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(
            status_code=503,
            detail="The speaker connected but its audio output did not become available. Try again in a few seconds.",
        ) from error

    save_result = subprocess.run(
        [
            "sudo",
            "-n",
            "/usr/local/bin/pimusic-set-bluetooth-device",
            address,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if save_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=save_result.stdout.strip()
            or save_result.stderr.strip()
            or "Failed to save preferred Bluetooth device",
        )

    return bluetooth_info(address)

@app.post("/api/player/play-playlist/{playlist_id}")
def play_playlist(playlist_id: str):
    params = navidrome_params()
    params["id"] = playlist_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getPlaylist.view",
        params=params,
        timeout=10.0,
    )

    response.raise_for_status()

    playlist = response.json()["subsonic-response"]["playlist"]

    songs = playlist.get("entry", [])

    result = play_song_queue(songs)

    return {
        "status": result["status"],
        "queue_length": result["queue_length"],
        "playlist": playlist.get("name"),
    }

@app.post("/api/player/queue/{mode}/{collection}/{collection_id}")
def add_collection_to_queue(mode: str, collection: str, collection_id: str):
    if mode not in {"append", "mix"} or collection not in {"album", "playlist"}:
        raise HTTPException(status_code=404, detail="Unknown queue action")

    endpoint = "getAlbum.view" if collection == "album" else "getPlaylist.view"
    entry_key = "song" if collection == "album" else "entry"
    params = navidrome_params()
    params["id"] = collection_id
    response = httpx.get(f"{NAVIDROME_BASE}/rest/{endpoint}", params=params, timeout=10.0)
    response.raise_for_status()

    data = response.json()["subsonic-response"][collection]
    result = add_songs_to_queue(data.get(entry_key, []), mix=mode == "mix")
    return {**result, collection: data.get("name")}

@app.get("/api/player/queue")
def get_queue():
    sync_queue_from_mpv()
    state = queue_snapshot()
    return {"queue": [
        {**item, "position": index, "current": index == state["current_index"],
         "playing": index == state["current_index"] and not state["paused"]}
        for index, item in enumerate(state["items"])
    ]}

@app.post("/api/player/queue/{position}/play")
def play_queue_position(position: int):
    browser_active = browser_lease_active()
    state = queue_snapshot()
    if not state["items"]:
        raise HTTPException(
            status_code=400,
            detail="Queue is empty",
        )

    if position < 0 or position >= len(state["items"]):
        raise HTTPException(
            status_code=400,
            detail="Invalid queue position",
        )

    with queue_lock:
        queue_state.update({"current_index": position, "position": 0.0, "paused": False})
    save_queue_state()
    if browser_active:
        browser_playback.update({"position": 0.0, "paused": False})
        browser_state_changed()
    render_queue_to_mpv()

    return {
        "status": "playing",
        "position": position,
    }

@app.post("/api/player/queue/{position}/move")
def move_queue_position(position: int, request: QueueMoveRequest):
    state = queue_snapshot()
    target = request.target_position
    if not (0 <= position < len(state["items"]) and 0 <= target < len(state["items"])):
        raise HTTPException(status_code=400, detail="Invalid queue position")
    if [item["queue_id"] for item in state["items"]] != request.queue_ids:
        raise HTTPException(status_code=409, detail="Queue changed. Please try again.")

    if position != target:
        current_id = state["items"][state["current_index"]]["queue_id"] if state["current_index"] >= 0 else None
        with queue_lock:
            item = queue_state["items"].pop(position)
            queue_state["items"].insert(target, item)
            queue_state["current_index"] = next((i for i, candidate in enumerate(queue_state["items"])
                                                  if candidate["queue_id"] == current_id), -1)
        save_queue_state()
        if not reorder_mpv_renderer(state["items"], queue_snapshot()["items"]):
            render_queue_to_mpv()
    return {"status": "moved", "position": target}

@app.delete("/api/player/queue")
def clear_queue():
    with queue_lock:
        queue_state.update({"items": [], "current_index": -1, "paused": True, "position": 0.0, "shuffle": False})
    save_queue_state()
    render_queue_to_mpv()
    return {"status": "cleared"}

@app.delete("/api/player/queue/{position}")
def remove_queue_position(position: int, queue_id: str):
    state = queue_snapshot()
    if position < 0 or position >= len(state["items"]):
        raise HTTPException(status_code=400, detail="Invalid queue position")
    if state["items"][position]["queue_id"] != queue_id:
        raise HTTPException(status_code=409, detail="Queue changed. Please try again.")
    was_current = position == state["current_index"]
    current_id = state["items"][state["current_index"]]["queue_id"] if state["current_index"] >= 0 else None
    with queue_lock:
        queue_state["items"].pop(position)
        if not queue_state["items"]:
            queue_state.update({"current_index": -1, "paused": True, "position": 0.0})
        elif was_current:
            queue_state.update({"current_index": min(position, len(queue_state["items"]) - 1), "position": 0.0})
        else:
            queue_state["current_index"] = next((i for i, item in enumerate(queue_state["items"])
                                                  if item["queue_id"] == current_id), -1)
    save_queue_state()
    if was_current or mpv_queue_song_ids() != [item["song_id"] for item in state["items"]]:
        render_queue_to_mpv()
    else:
        result = mpv_command(["playlist-remove", position])
        if result.get("error") != "success":
            render_queue_to_mpv()
    if browser_lease_active() and was_current:
        browser_playback["position"] = 0.0
        browser_state_changed()

    return {"status": "removed", "position": position}

@app.get("/api/library/playlists/{playlist_id}")
def get_playlist(playlist_id: str):
    params = navidrome_params()
    params["id"] = playlist_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getPlaylist.view",
        params=params,
        timeout=10.0,
    )
    response.raise_for_status()

    playlist = response.json()["subsonic-response"]["playlist"]

    return {
        "id": playlist["id"],
        "name": playlist["name"],
        "songCount": playlist.get("songCount", 0),
        "duration": playlist.get("duration", 0),
        "songs": [
            {
                "id": song["id"],
                "title": song["title"],
                "artist": song.get("artist"),
                "album": song.get("album"),
                "track": song.get("track"),
                "duration": song.get("duration"),
            }
            for song in playlist.get("entry", [])
        ],
    }

@app.get("/api/library/playlists")
def get_playlists():
    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getPlaylists.view",
        params=navidrome_params(),
        timeout=10.0,
    )
    response.raise_for_status()

    data = response.json()["subsonic-response"]

    playlists = []

    for playlist in data.get("playlists", {}).get("playlist", []):
        playlists.append({
            "id": playlist["id"],
            "name": playlist["name"],
            "songCount": playlist.get("songCount", 0),
            "duration": playlist.get("duration", 0),
        })

    return {
        "playlists": playlists
    }

@app.get("/api/library/search")
def search_library(q: str):
    params = navidrome_params()
    params.update({
        "query": q,
        "artistCount": 20,
        "albumCount": 20,
        "songCount": 50,
    })

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/search3.view",
        params=params,
        timeout=10.0,
    )
    response.raise_for_status()

    result = response.json()["subsonic-response"].get("searchResult3", {})

    return {
        "artists": [
            {
                "id": artist["id"],
                "name": artist["name"],
                "albumCount": artist.get("albumCount", 0),
            }
            for artist in result.get("artist", [])
        ],
        "albums": [
            {
                "id": album["id"],
                "name": album["name"],
                "artist": album.get("artist"),
                "year": album.get("year"),
            }
            for album in result.get("album", [])
        ],
        "songs": [
            {
                "id": song["id"],
                "title": song["title"],
                "artist": song.get("artist"),
                "album": song.get("album"),
                "track": song.get("track"),
                "duration": song.get("duration"),
            }
            for song in result.get("song", [])
        ],
    }

@app.get("/api/library/albums/{album_id}")
def get_album(album_id: str):
    params = navidrome_params()
    params["id"] = album_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getAlbum.view",
        params=params,
        timeout=10.0,
    )
    response.raise_for_status()

    album = response.json()["subsonic-response"]["album"]

    return {
        "id": album["id"],
        "name": album["name"],
        "artist": album.get("artist"),
        "year": album.get("year"),
        "songs": [
            {
                "id": song["id"],
                "title": song["title"],
                "artist": song.get("artist"),
                "track": song.get("track"),
                "duration": song.get("duration"),
            }
            for song in album.get("song", [])
        ],
    }

@app.post("/api/player/play-album/{album_id}")
def play_album(album_id: str):
    params = navidrome_params()
    params["id"] = album_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getAlbum.view",
        params=params,
        timeout=10.0,
    )

    response.raise_for_status()

    album = response.json()["subsonic-response"]["album"]

    songs = album.get("song", [])

    result = play_song_queue(songs)

    return {
        "status": result["status"],
        "queue_length": result["queue_length"],
        "album": album.get("name"),
    }

@app.get("/api/library/artists/{artist_id}")
def get_artist(artist_id: str):
    params = navidrome_params()
    params["id"] = artist_id

    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getArtist.view",
        params=params,
        timeout=10.0,
    )
    response.raise_for_status()

    artist = response.json()["subsonic-response"]["artist"]

    return {
        "id": artist["id"],
        "name": artist["name"],
        "albums": [
            {
                "id": album["id"],
                "name": album["name"],
                "year": album.get("year"),
                "songCount": album.get("songCount", 0),
                "coverArt": album.get("coverArt"),
            }
            for album in artist.get("album", [])
        ],
    }

@app.get("/api/library/artists")
def get_artists():
    response = httpx.get(
        f"{NAVIDROME_BASE}/rest/getArtists.view",
        params=navidrome_params(),
        timeout=10.0,
    )
    response.raise_for_status()

    data = response.json()["subsonic-response"]

    artists = []

    for index in data.get("artists", {}).get("index", []):
        for artist in index.get("artist", []):
            artists.append({
                "id": artist["id"],
                "name": artist["name"],
                "albumCount": artist.get("albumCount", 0),
                "coverArt": artist.get("coverArt"),
            })

    return {
        "artists": artists
    }

@app.post("/api/player/next")
def next_track():
    browser_active = browser_lease_active()
    state = queue_snapshot()
    if state["current_index"] < 0:
        raise HTTPException(status_code=400, detail="No next track")
    if mpv_property("loop-file") == "inf":
        target = state["current_index"]
    elif state["current_index"] + 1 < len(state["items"]):
        target = state["current_index"] + 1
    elif mpv_property("loop-playlist") == "inf":
        target = 0
    else:
        raise HTTPException(status_code=400, detail="No next track")
    with queue_lock:
        queue_state.update({"current_index": target, "position": 0.0})
    save_queue_state()
    if browser_active:
        browser_playback.update({"position": 0.0, "paused": False})
        browser_state_changed()
    render_queue_to_mpv()
    return {"status": "ok"}


@app.post("/api/player/previous")
def previous_track():
    browser_active = browser_lease_active()
    state = queue_snapshot()
    if state["current_index"] < 0:
        raise HTTPException(status_code=400, detail="No previous track")
    if state["current_index"] > 0:
        target = state["current_index"] - 1
    elif mpv_property("loop-playlist") == "inf":
        target = len(state["items"]) - 1
    else:
        raise HTTPException(status_code=400, detail="No previous track")
    with queue_lock:
        queue_state.update({"current_index": target, "position": 0.0})
    save_queue_state()
    if browser_active:
        browser_playback.update({"position": 0.0, "paused": False})
        browser_state_changed()
    render_queue_to_mpv()
    return {"status": "ok"}

@app.post("/api/player/shuffle")
def toggle_shuffle():
    state = queue_snapshot()
    if state["current_index"] < 0:
        raise HTTPException(status_code=400, detail="Queue is empty")
    with queue_lock:
        upcoming = queue_state["items"][queue_state["current_index"] + 1:]
        random.shuffle(upcoming)
        queue_state["items"][queue_state["current_index"] + 1:] = upcoming
        queue_state["shuffle"] = True
    save_queue_state()
    if not reorder_mpv_renderer(state["items"], queue_snapshot()["items"]):
        render_queue_to_mpv()

    return {
        "shuffle": True
    }

@app.get("/api/player/shuffle")
def get_shuffle():
    return {"shuffle": queue_snapshot()["shuffle"]}

@app.post("/api/player/repeat")
def cycle_repeat():
    loop_file = mpv_property("loop-file")
    loop_playlist = mpv_property("loop-playlist")

    # Off -> Repeat All
    if loop_file in (False, "no", None) and loop_playlist in (False, "no", None):
        mpv_command(["set_property", "loop-playlist", "inf"])
        mpv_command(["set_property", "loop-file", "no"])
        mode = "all"

    # Repeat All -> Repeat One
    elif loop_playlist == "inf":
        mpv_command(["set_property", "loop-playlist", "no"])
        mpv_command(["set_property", "loop-file", "inf"])
        mode = "one"

    # Repeat One -> Off
    else:
        mpv_command(["set_property", "loop-playlist", "no"])
        mpv_command(["set_property", "loop-file", "no"])
        mode = "off"

    return {"repeat": mode}

@app.get("/api/player/repeat")
def get_repeat():
    loop_file = mpv_property("loop-file")
    loop_playlist = mpv_property("loop-playlist")

    if loop_file == "inf":
        mode = "one"
    elif loop_playlist == "inf":
        mode = "all"
    else:
        mode = "off"

    return {"repeat": mode}

@app.post("/api/player/seek")
def seek(request: SeekRequest):
    position = max(0, request.position)
    with queue_lock:
        queue_state["position"] = position
    save_queue_state()
    result = mpv_command(["set_property", "time-pos", position])
    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    if browser_lease_active():
        pause_mpv_for_browser()
        browser_playback["position"] = position
        browser_state_changed()
    return {
        "status": "ok",
        "position": position
    }

@app.post("/api/player/volume")
def set_volume(request: VolumeRequest):
    volume = max(0, min(100, request.volume))

    result = mpv_command(["set_property", "volume", volume])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    save_volume_state(volume)

    return {
        "status": "ok",
        "volume": volume
    }

@app.post("/api/player/play-next/{song_id}")
def play_next_song(song_id: str):
    song = navidrome_song(song_id)
    state = queue_snapshot()
    if not state["items"] or state["current_index"] < 0:
        result = play_song_queue([song])
        return {"status": result["status"], "song_id": song_id, "inserted": False}
    with queue_lock:
        addition = queue_item_from_song(song)
        queue_state["items"].insert(queue_state["current_index"] + 1, addition)
        queue_state["shuffle"] = False
    save_queue_state()
    if mpv_queue_song_ids() == [item["song_id"] for item in state["items"]]:
        result = mpv_command(["loadfile", navidrome_stream_url(addition["song_id"]), "insert-next"])
        if result.get("error") != "success":
            render_queue_to_mpv()
    else:
        render_queue_to_mpv()
    return {"status": "queued_next", "song_id": song_id, "inserted": True}

@app.post("/api/player/play/{song_id}")
def play_song(song_id: str):
    song = navidrome_song(song_id)
    result = play_song_queue([song])

    return {
        "status": result["status"],
        "song_id": song_id
    }

@app.get("/api/status")
def status():
    browser_active = browser_lease_active()
    if not browser_active:
        sync_queue_from_mpv()
    state = queue_snapshot()
    items = state["items"]
    current_index = state["current_index"]
    current = items[current_index] if 0 <= current_index < len(items) else {}
    position = browser_playback["position"] if browser_active else state["position"]
    duration = current.get("duration") or (mpv_property("duration") or 0)
    total_runtime = 0
    upcoming_runtime = 0
    up_next = None

    for index, item in enumerate(items):
        song_duration = item.get("duration") or 0
        total_runtime += song_duration
        if index > current_index:
            upcoming_runtime += song_duration
        if index == current_index + 1:
            up_next = {
                "title": item.get("title"), "artist": item.get("artist"),
            }

    remaining_runtime = upcoming_runtime
    if current_index >= 0:
        remaining_runtime += max(0, duration - position)

    return {
        "name": "PiMusic",
        "status": "ok",
        "player": {
            "idle": not bool(items),
            "paused": browser_playback["paused"] if browser_active else state["paused"],
            "title": current.get("title"), "artist": current.get("artist"),
            "album": current.get("album"), "song_id": current.get("song_id"),
            "coverArt": current.get("coverArt"),
            "position": position,
            "duration": duration,
            "volume": mpv_property("volume"),
            "output": "browser" if browser_active else "pi",
            "revision": browser_playback["revision"] if browser_active else 0,
        },
        "queue": {
            "up_next": up_next,
            "tracks_remaining": max(0, len(items) - current_index - 1),
            "remaining_runtime": remaining_runtime,
            "total_runtime": total_runtime,
        },
    }

@app.post("/api/player/play")
def play():
    if browser_lease_active():
        browser_playback["paused"] = False
        with queue_lock:
            queue_state["paused"] = False
        save_queue_state()
        pause_mpv_for_browser()
        browser_state_changed()
        return {"status": "playing"}
    result = mpv_command(["set_property", "pause", False])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    with queue_lock:
        queue_state["paused"] = False
    save_queue_state()
    return {"status": "playing"}


@app.post("/api/player/pause")
def pause():
    if browser_lease_active():
        browser_playback["paused"] = True
        with queue_lock:
            queue_state["paused"] = True
        save_queue_state()
        pause_mpv_for_browser()
        browser_state_changed()
        return {"status": "paused"}
    result = mpv_command(["set_property", "pause", True])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    with queue_lock:
        queue_state["paused"] = True
    save_queue_state()
    return {"status": "paused"}

@app.get("/api/bluetooth/status")
def bluetooth_status():
    result = subprocess.run(
        ["bluetoothctl", "devices", "Connected"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line.startswith("Device "):
            continue

        parts = line.split(" ", 2)

        if len(parts) >= 2:
            info = bluetooth_info(parts[1])
            if info["audio"]:
                return info

    return {
        "address": None,
        "name": None,
        "connected": False,
        "paired": False,
        "trusted": False,
    }

@app.get("/api/wifi/status")
def wifi_status():
    return wifi_status_info()

@app.get("/api/wifi/setup")
def wifi_setup_status():
    return hotspot_setup_status()

@app.post("/api/wifi/setup/{action}")
def wifi_setup_action(action: str):
    if action not in ("start", "stop"):
        raise HTTPException(status_code=404, detail="Unknown setup action")
    return hotspot_setup_status(action)

@app.get("/api/system/status")
def system_status():
    try:
        mpv_available = mpv_command(["get_property", "idle-active"]).get("error") == "success"
    except (OSError, RuntimeError):
        mpv_available = False
    try:
        wifi = wifi_status_info()
    except (OSError, subprocess.TimeoutExpired):
        wifi = {"available": False, "connected": False, "ip_address": None}
    try:
        bluetooth = bluetooth_status()
    except (OSError, subprocess.TimeoutExpired):
        bluetooth = {"connected": False, "name": None}
    return {
        "version": PIMUSIC_VERSION,
        "mpv_available": mpv_available,
        "wifi_ip": wifi.get("ip_address"),
        "speaker": bluetooth.get("name") if bluetooth.get("connected") else None,
    }

@app.post("/api/wifi/scan")
def wifi_scan():
    return wifi_networks()

@app.post("/api/wifi/connect")
def wifi_connect(request: WifiConnectRequest):
    return connect_wifi(request)

@app.get("/api/wifi/saved")
def wifi_saved_connections():
    return saved_wifi_connections()

def list_bluetooth_devices():
    result = subprocess.run(
        ["bluetoothctl", "devices"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    devices = []

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line.startswith("Device "):
            continue

        parts = line.split(" ", 2)

        if len(parts) < 3:
            continue

        address = parts[1]
        fallback_name = parts[2]

        info = bluetooth_info(address)

        if not info["audio"]:
            continue

        devices.append({
            "address": address,
            "name": info["name"] or fallback_name,
            "paired": info["paired"],
            "trusted": info["trusted"],
            "connected": info["connected"],
        })

    return {
        "devices": devices
    }


@app.get("/api/bluetooth/devices")
def bluetooth_devices():
    return list_bluetooth_devices()


@app.post("/api/bluetooth/scan")
def bluetooth_scan():
    if not bluetooth_scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Bluetooth is already scanning. Wait for it to finish.")
    try:
        try:
            result = subprocess.run(
                ["bluetoothctl", "--timeout", "30", "scan", "on"],
                capture_output=True,
                text=True,
                timeout=35,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HTTPException(status_code=503, detail="Bluetooth scan timed out. Try again.") from error
        finally:
            # BlueZ should stop when bluetoothctl's timeout expires, but make a
            # best-effort stop so a later UI scan cannot inherit discovery.
            try:
                subprocess.run(
                    ["bluetoothctl", "scan", "off"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        if result.returncode != 0:
            detail = bluetooth_failure_message(
                result,
                "Bluetooth scan failed. Try restarting Bluetooth if the problem continues.",
            )
            raise HTTPException(status_code=409 if "already scanning" in detail.lower() else 503, detail=detail)

        return list_bluetooth_devices()
    finally:
        bluetooth_scan_lock.release()

@app.post("/api/bluetooth/pair-device")
def bluetooth_pair_device(request: BluetoothDeviceRequest):
    address = request.address

    info = bluetooth_info(address)

    if not info["paired"]:
        pair_result = subprocess.run(
            ["bluetoothctl", "pair", address],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if pair_result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=pair_result.stdout.strip()
                or pair_result.stderr.strip()
                or "Bluetooth pairing failed",
            )

    trust_result = subprocess.run(
        ["bluetoothctl", "trust", address],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if trust_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=trust_result.stdout.strip()
            or trust_result.stderr.strip()
            or "Bluetooth trust failed",
        )

    connect_result = subprocess.run(
        ["bluetoothctl", "connect", address],
        capture_output=True,
        text=True,
        timeout=20,
    )

    if connect_result.returncode != 0 or "Connection successful" not in connect_result.stdout:
        raise HTTPException(
            status_code=500,
            detail=connect_result.stdout.strip()
            or connect_result.stderr.strip()
            or "Bluetooth connection failed",
        )

    switch_bluetooth_output(address)

    save_result = subprocess.run(
        [
            "sudo",
            "-n",
            "/usr/local/bin/pimusic-set-bluetooth-device",
            address,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if save_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=save_result.stdout.strip()
            or save_result.stderr.strip()
            or "Connected, but failed to save preferred Bluetooth device",
        )

    return bluetooth_info(address)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
