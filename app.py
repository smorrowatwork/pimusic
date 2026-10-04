import json
import socket
import httpx
import subprocess
import time
import os
import re
import tempfile
import random
from threading import RLock

from urllib.parse import urlparse, parse_qs
from pathlib import Path
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel


NAVIDROME_BASE = os.environ["NAVIDROME_BASE"]
NAVIDROME_USER = os.environ["NAVIDROME_USER"]
NAVIDROME_SALT = os.environ["NAVIDROME_SALT"]
NAVIDROME_TOKEN = os.environ["NAVIDROME_TOKEN"]

app = FastAPI(title="PiMusic")

QUEUE_METADATA_FILE = Path("/home/stevenm/.config/pimusic/queue.json")
SHUFFLE_STATE_FILE = QUEUE_METADATA_FILE.with_name("shuffle.json")
PLAYBACK_STATE_FILE = QUEUE_METADATA_FILE.with_name("playback.json")
VOLUME_STATE_FILE = QUEUE_METADATA_FILE.with_name("volume.json")
queue_metadata = {}
shuffle_enabled = False
last_playback_state = None

class VolumeRequest(BaseModel):
    volume: float

class SeekRequest(BaseModel):
    position: float

class QueueMoveRequest(BaseModel):
    target_position: int
    queue_ids: list[int]

class BluetoothDeviceRequest(BaseModel):
    address: str

class WifiConnectRequest(BaseModel):
    ssid: str
    password: str | None = None

MPV_SOCKET = "/tmp/pimusic-mpv.sock"
mpv_lock = RLock()
PIMUSIC_VERSION = "1.0"

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
            raise HTTPException(status_code=400, detail="Unable to prepare that Wi-Fi connection")

    if request.password:
        secured = subprocess.run(
            ["nmcli", "connection", "modify", profile, "wifi-sec.key-mgmt", "wpa-psk"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if secured.returncode != 0:
            raise HTTPException(status_code=400, detail="Unable to prepare that Wi-Fi connection")

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
    finally:
        if password_file:
            Path(password_file).unlink(missing_ok=True)
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail="Unable to connect to that Wi-Fi network")
    return wifi_status_info()

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

def mpv_socket_identity():
    stat = Path(MPV_SOCKET).stat()
    return [stat.st_ino, stat.st_ctime_ns]

def save_shuffle_state():
    state = {"enabled": shuffle_enabled}
    if shuffle_enabled:
        playlist = mpv_property("playlist")
        if playlist is None:
            raise HTTPException(status_code=500, detail="Unable to save shuffle state")
        state.update(socket=mpv_socket_identity(), ids=[item["id"] for item in playlist])
    SHUFFLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SHUFFLE_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(SHUFFLE_STATE_FILE)

def load_shuffle_state():
    global shuffle_enabled
    shuffle_enabled = False
    try:
        state = json.loads(SHUFFLE_STATE_FILE.read_text(encoding="utf-8"))
        if state.get("enabled") is not True or state.get("socket") != mpv_socket_identity():
            return
        playlist = mpv_property("playlist")
        if playlist is None:
            raise HTTPException(status_code=500, detail="Unable to read shuffle state")
        # Removals and manual reordering can preserve a shuffled queue.
        shuffle_enabled = bool(playlist) and {item["id"] for item in playlist}.issubset(set(state["ids"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        shuffle_enabled = False

def save_queue_metadata():
    QUEUE_METADATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with QUEUE_METADATA_FILE.open("w") as f:
        json.dump(queue_metadata, f)


def load_queue_metadata():
    global queue_metadata

    if not QUEUE_METADATA_FILE.exists():
        queue_metadata = {}
        return

    try:
        with QUEUE_METADATA_FILE.open("r") as f:
            queue_metadata = json.load(f)
    except Exception:
        queue_metadata = {}

def save_playback_state(playlist):
    global last_playback_state
    song_ids = []
    for item in playlist:
        try:
            song_id = parse_qs(urlparse(item.get("filename", "")).query).get("id", [None])[0]
            if song_id:
                song_ids.append(song_id)
        except Exception:
            continue
    state = {
        "song_ids": song_ids,
        "position": mpv_property("playlist-pos"),
        "paused": mpv_property("pause") is True,
    }
    if state == last_playback_state:
        return
    PLAYBACK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PLAYBACK_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(PLAYBACK_STATE_FILE)
    last_playback_state = state

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

def cache_queue_song_metadata(song):
    song_id = song["id"]
    cover_art = song.get("coverArt")

    if not cover_art:
        try:
            cover_art = navidrome_song(song_id).get("coverArt")
        except Exception:
            cover_art = None

    queue_metadata[song_id] = {
        "title": song.get("title"),
        "artist": song.get("artist"),
        "album": song.get("album"),
        "coverArt": cover_art,
        "duration": song.get("duration"),
    }

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

def play_song_queue(songs):

    global queue_metadata
    global shuffle_enabled

    queue_metadata = {}
    for song in songs:
        cache_queue_song_metadata(song)

    save_queue_metadata()

    song_ids = [song["id"] for song in songs]

    if not song_ids:
        raise HTTPException(
            status_code=400,
            detail="Queue contains no songs",
        )

    with mpv_lock:
        clear_mpv_playlist()

        first_url = navidrome_stream_url(song_ids[0])

        result = mpv_command([
            "loadfile",
            first_url,
            "replace",
        ])

        if result.get("error") != "success":
            raise HTTPException(
                status_code=500,
                detail=result,
            )

        shuffle_enabled = False
        save_shuffle_state()

        for song_id in song_ids[1:]:
            stream_url = navidrome_stream_url(song_id)

            result = mpv_command([
                "loadfile",
                stream_url,
                "append",
            ])

            if result.get("error") != "success":
                raise HTTPException(
                    status_code=500,
                    detail=result,
                )

        mpv_command(["set_property", "pause", False])

    return {
        "status": "playing",
        "queue_length": len(song_ids),
    }

def add_songs_to_queue(songs, mix=False):
    global shuffle_enabled

    if not songs:
        raise HTTPException(status_code=400, detail="Collection contains no songs")

    with mpv_lock:
        playlist = mpv_property("playlist") or []
        current_position = mpv_property("playlist-pos")
        track_is_paused = mpv_property("pause") is True
        track_is_playing = mpv_property("core-idle") is False

        if current_position is None or current_position < 0 or not (track_is_paused or track_is_playing):
            return play_song_queue(songs)

        for song in songs:
            cache_queue_song_metadata(song)
            result = mpv_command(["loadfile", navidrome_stream_url(song["id"]), "append"])
            if result.get("error") != "success":
                raise HTTPException(status_code=500, detail="Unable to add song to the queue")

            if mix:
                playlist = mpv_property("playlist") or []
                source_position = len(playlist) - 1
                target_position = random.randint(current_position + 1, source_position)
                if source_position != target_position:
                    result = mpv_command(["playlist-move", source_position, target_position])
                    if result.get("error") != "success":
                        raise HTTPException(status_code=500, detail="Unable to mix song into the queue")

        shuffle_enabled = False
        save_shuffle_state()
        save_queue_metadata()

    return {"status": "mixed" if mix else "appended", "queue_length": len(songs)}

# Runs at startup #
# Load the queue from persistent storage
load_queue_metadata()

@app.on_event("startup")
def restore_playback_queue():
    restore_volume_state()
    try:
        if mpv_property("playlist"):
            return
        state = json.loads(PLAYBACK_STATE_FILE.read_text(encoding="utf-8"))
        song_ids = state.get("song_ids", [])
        if not song_ids:
            return
        songs = [navidrome_song(song_id) for song_id in song_ids]
        play_song_queue(songs)
        position = state.get("position", 0)
        if isinstance(position, int) and 0 <= position < len(songs):
            mpv_command(["set_property", "playlist-pos", position])
        if state.get("paused"):
            mpv_command(["set_property", "pause", True])
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

@app.post("/api/bluetooth/connect-device")
def bluetooth_connect_device(request: BluetoothDeviceRequest):
    address = request.address

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
                status_code=500,
                detail=result.stdout.strip()
                or result.stderr.strip()
                or "Bluetooth connection failed",
            )

    switch_bluetooth_output(address)

    save_result = subprocess.run(
        [
            "sudo",
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
    result = mpv_command(["get_property", "playlist"])

    if result.get("error") != "success":
        raise HTTPException(
            status_code=500,
            detail=result,
        )

    playlist = result.get("data") or []
    queue = []

    for index, item in enumerate(playlist):
        filename = item.get("filename", "")
        song_id = None

        try:
            query = parse_qs(urlparse(filename).query)
            song_id = query.get("id", [None])[0]
        except Exception:
            pass

        metadata = queue_metadata.get(song_id, {})

        queue.append({
            "position": index,
            "song_id": song_id,
            "title": metadata.get("title") or item.get("title"),
            "artist": metadata.get("artist"),
            "album": metadata.get("album"),
            "current": item.get("current", False),
            "playing": item.get("playing", False),
            "mpv_id": item.get("id"),
        })

    return {
        "queue": queue
    }

@app.post("/api/player/queue/{position}/play")
def play_queue_position(position: int):
    playlist = mpv_property("playlist")

    if not playlist:
        raise HTTPException(
            status_code=400,
            detail="Queue is empty",
        )

    if position < 0 or position >= len(playlist):
        raise HTTPException(
            status_code=400,
            detail="Invalid queue position",
        )

    result = mpv_command([
        "set_property",
        "playlist-pos",
        position,
    ])

    if result.get("error") != "success":
        raise HTTPException(
            status_code=500,
            detail=result,
        )

    mpv_command(["set_property", "pause", False])

    return {
        "status": "playing",
        "position": position,
    }

@app.post("/api/player/queue/{position}/move")
def move_queue_position(position: int, request: QueueMoveRequest):
    playlist = mpv_property("playlist")
    if playlist is None:
        raise HTTPException(status_code=500, detail="Unable to read queue")

    target = request.target_position
    if not (0 <= position < len(playlist) and 0 <= target < len(playlist)):
        raise HTTPException(status_code=400, detail="Invalid queue position")
    if [item.get("id") for item in playlist] != request.queue_ids:
        raise HTTPException(status_code=409, detail="Queue changed. Please try again.")

    if position != target:
        # MPV inserts before the target entry in the original playlist.
        insertion = target + 1 if position < target else target
        result = mpv_command(["playlist-move", position, insertion])
        if result.get("error") != "success":
            raise HTTPException(status_code=500, detail="Unable to move queue item")
    return {"status": "moved", "position": target}

@app.delete("/api/player/queue")
def clear_queue():
    global queue_metadata, shuffle_enabled

    with mpv_lock:
        clear_mpv_playlist()

    queue_metadata = {}
    shuffle_enabled = False
    save_shuffle_state()
    save_queue_metadata()
    return {"status": "cleared"}

@app.delete("/api/player/queue/{position}")
def remove_queue_position(position: int, mpv_id: int):
    playlist = mpv_property("playlist")

    if playlist is None:
        raise HTTPException(status_code=500, detail="Unable to read queue")

    if position < 0 or position >= len(playlist):
        raise HTTPException(status_code=400, detail="Invalid queue position")

    if playlist[position].get("id") != mpv_id:
        raise HTTPException(status_code=409, detail="Queue changed. Please try again.")

    result = mpv_command(["playlist-remove", position])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail="Unable to remove queue item")

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
    result = mpv_command(["playlist-next", "force"])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    return {"status": "ok"}


@app.post("/api/player/previous")
def previous_track():
    result = mpv_command(["playlist-prev", "force"])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    return {"status": "ok"}

@app.post("/api/player/shuffle")
def toggle_shuffle():
    global shuffle_enabled
    with mpv_lock:
        playlist = mpv_property("playlist") or []
        current_position = mpv_property("playlist-pos")
        if current_position is None or current_position < 0:
            raise HTTPException(status_code=400, detail="Queue is empty")

        upcoming = playlist[current_position + 1:]
        random.shuffle(upcoming)
        for target_position, item in enumerate(upcoming, start=current_position + 1):
            source_position = next(
                index for index, candidate in enumerate(playlist)
                if candidate["id"] == item["id"]
            )
            if source_position != target_position:
                result = mpv_command(["playlist-move", source_position, target_position])
                if result.get("error") != "success":
                    raise HTTPException(status_code=500, detail="Unable to shuffle upcoming tracks")
                moved = playlist.pop(source_position)
                playlist.insert(target_position, moved)

    shuffle_enabled = True
    save_shuffle_state()

    return {
        "shuffle": shuffle_enabled
    }

@app.get("/api/player/shuffle")
def get_shuffle():
    load_shuffle_state()
    return {
        "shuffle": shuffle_enabled
    }

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

    result = mpv_command(["set_property", "time-pos", position])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

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
    with mpv_lock:
        playlist = mpv_property("playlist")
        track_is_paused = mpv_property("pause") is True
        track_is_playing = mpv_property("core-idle") is False

        if not playlist or not (track_is_paused or track_is_playing):
            result = play_song_queue([song])
            return {
                "status": result["status"],
                "song_id": song_id,
                "inserted": False,
            }

        cache_queue_song_metadata(song)
        result = mpv_command([
            "loadfile",
            navidrome_stream_url(song_id),
            "insert-next",
        ])

        if result.get("error") != "success":
            raise HTTPException(
                status_code=500,
                detail="Unable to add song to the queue",
            )

        save_queue_metadata()

        return {
            "status": "queued_next",
            "song_id": song_id,
            "inserted": True,
        }

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
    metadata = mpv_metadata()

    playlist_result = mpv_command(["get_property", "playlist"])
    playlist = playlist_result.get("data") or []
    save_playback_state(playlist)

    current_song_id = None
    cover_art = None
    current_index = next((index for index, item in enumerate(playlist) if item.get("current")), -1)

    if current_index >= 0:
        item = playlist[current_index]
        filename = item.get("filename", "")

        try:
            query = parse_qs(urlparse(filename).query)
            current_song_id = query.get("id", [None])[0]
        except Exception:
            current_song_id = None

        if current_song_id:
            cover_art = queue_metadata.get(current_song_id, {}).get("coverArt")

    position = mpv_property("time-pos") or 0
    duration = mpv_property("duration") or 0
    total_runtime = 0
    upcoming_runtime = 0
    up_next = None

    for index, item in enumerate(playlist):
        try:
            song_id = parse_qs(urlparse(item.get("filename", "")).query).get("id", [None])[0]
        except Exception:
            song_id = None
        song_duration = queue_metadata.get(song_id, {}).get("duration") or 0
        total_runtime += song_duration
        if index > current_index:
            upcoming_runtime += song_duration
        if index == current_index + 1:
            metadata_for_next = queue_metadata.get(song_id, {})
            up_next = {
                "title": metadata_for_next.get("title") or item.get("title"),
                "artist": metadata_for_next.get("artist"),
            }

    remaining_runtime = upcoming_runtime
    if current_index >= 0:
        remaining_runtime += max(0, duration - position)

    return {
        "name": "PiMusic",
        "status": "ok",
        "player": {
            "idle": mpv_property("idle-active"),
            "paused": mpv_property("pause"),
            "title": metadata.get("title") or mpv_property("media-title"),
            "artist": metadata.get("artist"),
            "album": metadata.get("album"),
            "song_id": current_song_id,
            "coverArt": cover_art,
            "position": position,
            "duration": duration,
            "volume": mpv_property("volume")
        },
        "queue": {
            "up_next": up_next,
            "tracks_remaining": max(0, len(playlist) - current_index - 1),
            "remaining_runtime": remaining_runtime,
            "total_runtime": total_runtime,
        },
    }

@app.post("/api/player/play")
def play():
    result = mpv_command(["set_property", "pause", False])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

    return {"status": "playing"}


@app.post("/api/player/pause")
def pause():
    result = mpv_command(["set_property", "pause", True])

    if result.get("error") != "success":
        raise HTTPException(status_code=500, detail=result)

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
    result = subprocess.run(
        ["bluetoothctl", "--timeout", "10", "scan", "on"],
        capture_output=True,
        text=True,
        timeout=15,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=result.stdout.strip()
            or result.stderr.strip()
            or "Bluetooth scan failed",
        )

    return list_bluetooth_devices()

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
