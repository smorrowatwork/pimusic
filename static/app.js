
    const seek = document.getElementById("seek");
    const volume = document.getElementById("volume");
    const title = document.getElementById("title");
    const artist = document.getElementById("artist");
    const album = document.getElementById("album");
    const upNext = document.getElementById("up-next");
    const queueRuntime = document.getElementById("queue-runtime");

const elapsedTime = document.getElementById("elapsed-time");
const totalTime = document.getElementById("total-time");
const coverArt = document.getElementById("coverArt");
const playPauseButton = document.getElementById("playPauseButton");
const browserAudio = document.getElementById("browser-audio");
const outputStatus = document.getElementById("output-status");
const browserOutputButton = document.getElementById("browser-output-button");
const piOutputButton = document.getElementById("pi-output-button");

let lastCurrentSongId = null;
let queueMovePending = false;
let queueDrag = null;
let queueLoadVersion = 0;
let selectedWifiNetwork = null;
let messageTimer = null;
let playNextPending = false;
let wifiSetupPending = false;
let wifiConnectPending = false;
let savedWifiNetworksExpanded = false;
let savedBluetoothDevicesExpanded = false;
let browserLease = sessionStorage.getItem("pimusic-browser-lease");
let browserSongId = null;
let browserOutputActive = false;
let browserProgressPending = false;
let browserRevision = null;
let statusRefreshPending = false;
let resumeRefreshPending = false;
const API_REQUEST_TIMEOUT_MS = 15000;
const BLUETOOTH_SCAN_TIMEOUT_MS = 45000;

function showError(error) {
  const message = document.getElementById("app-message");
  message.textContent = error?.message || "Something went wrong. Please try again.";
  message.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { message.hidden = true; }, 6000);
}

async function apiJson(url, options, timeoutMs = API_REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let data;
  try {
    response = await fetch(url, { ...options, signal: controller.signal });
    const contentType = response.headers.get("content-type") || "";
    data = contentType.includes("application/json")
      ? await response.json()
      : { detail: await response.text() };
  } catch (cause) {
    const error = new Error(
      cause?.name === "AbortError"
        ? "PiMusic did not respond. Check the connection and try again."
        : "Unable to reach PiMusic. Check the network connection."
    );
    error.networkError = true;
    throw error;
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) throw new Error(data.detail || "The request could not be completed.");
  return data;
}

async function startPlayback(url) {
  try {
    await apiJson(url, { method: "POST" });
    await refreshStatus();
    await loadQueue();
  } catch (error) {
    showError(error);
  }
}

async function playNextSong(songId) {
  if (playNextPending) return;
  playNextPending = true;
  try {
    await apiJson(`/api/player/play-next/${songId}`, { method: "POST" });
    await refreshStatus();
    await loadQueue();
  } catch (error) {
    showError(error);
  } finally {
    playNextPending = false;
  }
}

async function addCollectionToQueue(mode, collection, collectionId) {
  try {
    await apiJson(`/api/player/queue/${mode}/${collection}/${collectionId}`, { method: "POST" });
    await refreshStatus();
    await loadQueue();
  } catch (error) {
    showError(error);
  }
}

function appendCollectionActions(container, collection, collectionId, playUrl) {
  const actions = document.createElement("div");
  actions.className = "collection-actions";
  for (const [label, action] of [
    ["Play", () => startPlayback(playUrl)],
    ["Append to Queue", () => addCollectionToQueue("append", collection, collectionId)],
    ["Mix into Queue", () => addCollectionToQueue("mix", collection, collectionId)],
  ]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.onclick = async () => {
      button.disabled = true;
      try { await action(); } finally { button.disabled = false; }
    };
    actions.appendChild(button);
  }
  container.appendChild(actions);
}

function appendSongRow(container, labelText, songId) {
  const item = document.createElement("div");
  item.className = "song-row";
  item.onclick = () => startPlayback(`/api/player/play/${songId}`);

  const label = document.createElement("span");
  label.className = "song-row-label";
  label.textContent = labelText;

  const playNextButton = document.createElement("button");
  playNextButton.className = "song-play-next";
  playNextButton.textContent = "Play Next";
  playNextButton.title = "Add after the current track";
  playNextButton.onclick = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    playNextButton.disabled = true;
    try {
      await playNextSong(songId);
    } finally {
      playNextButton.disabled = false;
    }
  };

  item.append(label, playNextButton);
  container.appendChild(item);
}

function toggleQueue() {
  const card = document.getElementById("queue-card");
  const button = document.getElementById("queueToggleButton");
  const collapsed = card.classList.toggle("is-collapsed");
  button.textContent = collapsed ? "Expand" : "Collapse";
  button.setAttribute("aria-expanded", String(!collapsed));
}

function toggleSection(cardId, buttonId) {
  const card = document.getElementById(cardId);
  const button = document.getElementById(buttonId);
  const collapsed = card.classList.toggle("is-collapsed");
  button.textContent = collapsed ? "Expand" : "Collapse";
  button.setAttribute("aria-expanded", String(!collapsed));
  if (cardId === "bluetooth-controls" && collapsed) {
    clearBluetoothScanResults();
    document.getElementById("bluetooth-scan-status").textContent = "";
  }
}

async function moveQueueItem(item, target, queueIds) {
  if (queueMovePending || item.position === target) return;
  queueMovePending = true;
  const container = document.getElementById("queue");
  container.classList.add("queue-busy");
  try {
    await apiJson(`/api/player/queue/${item.position}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_position: target, queue_ids: queueIds })
    });
  } catch (error) {
    showError(error);
  } finally {
    queueMovePending = false;
    container.classList.remove("queue-busy");
    await loadQueue();
  }
}

function addQueueDrag(handle, row, item, queueIds) {
  handle.onclick = event => event.stopPropagation();
  handle.onpointerdown = event => {
    if (event.button !== 0 || queueMovePending || queueDrag) return;
    event.preventDefault();
    event.stopPropagation();
    handle.setPointerCapture(event.pointerId);
    queueDrag = { pointerId: event.pointerId, target: item.position };
    row.classList.add("queue-dragging");
  };
  handle.onpointermove = event => {
    if (queueDrag?.pointerId !== event.pointerId) return;
    const rows = [...document.querySelectorAll("#queue > div")];
    let target = rows.findIndex(candidate => {
      const rect = candidate.getBoundingClientRect();
      return event.clientY < rect.top + rect.height / 2;
    });
    if (target < 0) target = rows.length;
    if (target > item.position) target--;
    queueDrag.target = target;
    rows.forEach((candidate, index) => candidate.classList.toggle("queue-drop-target", index === target));
    if (event.clientY < 60) window.scrollBy(0, -20);
    if (event.clientY > window.innerHeight - 60) window.scrollBy(0, 20);
  };
  const finish = async event => {
    if (queueDrag?.pointerId !== event.pointerId) return;
    const target = queueDrag.target;
    queueDrag = null;
    document.querySelectorAll("#queue > div").forEach(candidate => {
      candidate.classList.remove("queue-dragging", "queue-drop-target");
    });
    if (event.type === "pointerup") await moveQueueItem(item, target, queueIds);
    await loadQueue();
  };
  handle.onpointerup = finish;
  handle.onpointercancel = finish;
  handle.onlostpointercapture = finish;
}

async function togglePlayPause() {
  try {
    const data = await apiJson("/api/status");
    if (data.player.paused) {
      await play();
    } else {
      await pause();
    }
    await refreshStatus();
  } catch (error) {
    showError(error);
  }
}

async function refreshWifiStatus() {
  const name = document.getElementById("wifi-name");
  const status = document.getElementById("wifi-status");
  const details = document.getElementById("wifi-details");
  try {
    const response = await fetch("/api/wifi/status");
    const wifi = await response.json();
    if (!response.ok) throw new Error(wifi.detail || "Unavailable");
    if (!wifi.available) {
      name.textContent = "No Wi-Fi adapter";
      status.textContent = "Unavailable";
      details.textContent = "";
      return;
    }
    name.textContent = wifi.connection || wifi.device;
    status.textContent = wifi.connected ? "Connected" : wifi.state;
    details.textContent = wifi.ip_address ? `IP address: ${wifi.ip_address}` : "";
  } catch (error) {
    name.textContent = "Unavailable";
    status.textContent = "Unknown";
    details.textContent = "";
  }
}

function renderWifiSetupStatus(setup) {
  const status = document.getElementById("wifi-setup-status");
  const start = document.getElementById("wifi-setup-start");
  const stop = document.getElementById("wifi-setup-stop");
  if (!setup.installed) {
    status.textContent = "Setup hotspot control is not installed.";
    start.disabled = true;
    stop.disabled = true;
    return;
  }
  const stateMessages = {
    READY: "Ready: normal Wi-Fi is connected.",
    TRYING_WIFI: "Trying saved Wi-Fi networks…",
    PROVISIONING: setup.hotspot_active
      ? "Provisioning: setup hotspot is active."
      : "Provisioning: starting setup hotspot…",
    CONNECTING_WIFI: "Connecting to the selected Wi-Fi network…",
  };
  status.textContent = stateMessages[setup.state] || "Setup hotspot status unavailable.";
  start.disabled = wifiSetupPending || setup.forced || setup.state === "CONNECTING_WIFI";
  stop.disabled = wifiSetupPending || !setup.forced || setup.state === "CONNECTING_WIFI";
}

async function refreshWifiSetupStatus() {
  try {
    const setup = await apiJson("/api/wifi/setup");
    renderWifiSetupStatus(setup);
  } catch (error) {
    document.getElementById("wifi-setup-status").textContent = "Setup hotspot status unavailable.";
    document.getElementById("wifi-setup-start").disabled = true;
    document.getElementById("wifi-setup-stop").disabled = true;
  }
}

async function setWifiSetupMode(action) {
  if (wifiSetupPending) return;
  wifiSetupPending = true;
  document.getElementById("wifi-setup-start").disabled = true;
  document.getElementById("wifi-setup-stop").disabled = true;
  try {
    const setup = await apiJson(`/api/wifi/setup/${action}`, { method: "POST" });
    renderWifiSetupStatus(setup);
    await refreshWifiStatus();
  } catch (error) {
    showError(error);
    await refreshWifiSetupStatus();
  } finally {
    wifiSetupPending = false;
    await refreshWifiSetupStatus();
  }
}

async function refreshSystemStatus() {
  const container = document.getElementById("system-status");
  try {
    const data = await apiJson("/api/system/status");
    container.textContent = [
      `PiMusic ${data.version}`,
      `MPV: ${data.mpv_available ? "Available" : "Unavailable"}`,
      `Wi-Fi IP: ${data.wifi_ip || "Not connected"}`,
      `Speaker: ${data.speaker || "None connected"}`,
    ].join(" · ");
  } catch (error) {
    container.textContent = "System status unavailable.";
  }
}

async function loadNavidromeConfig() {
  const current = document.getElementById("navidrome-config-current");
  const base = document.getElementById("navidrome-base");
  try {
    const data = await apiJson("/api/navidrome/config");
    current.textContent = `Current server: ${data.base}`;
    base.value = data.base || "";
  } catch {
    current.textContent = "Current music-server configuration is unavailable.";
  }
}

async function saveNavidromeConfig(event) {
  event.preventDefault();
  const form = document.getElementById("navidrome-config-form");
  const button = form.querySelector("button[type=submit]");
  const status = document.getElementById("navidrome-config-status");
  if (!window.confirm("Switching music servers stops playback and clears the current queue. Continue?")) return;
  button.disabled = true;
  status.textContent = "Validating music server…";
  try {
    await apiJson("/api/navidrome/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base: document.getElementById("navidrome-base").value,
        user: document.getElementById("navidrome-user").value,
        password: document.getElementById("navidrome-password").value,
      })
    });
    document.getElementById("navidrome-password").value = "";
    status.textContent = "Music server changed. The queue was cleared.";
    await Promise.all([refreshStatus(), loadQueue(), loadPlaylists(), refreshSystemStatus(), loadNavidromeConfig()]);
  } catch (error) {
    status.textContent = error.message || "Unable to configure music server.";
    showError(error);
  } finally {
    button.disabled = false;
  }
}

async function loadSavedWifiNetworks() {
  const container = document.getElementById("wifi-saved-networks");
  container.textContent = "Loading saved networks…";
  try {
    const data = await apiJson("/api/wifi/saved");
    const connections = data.connections || [];
    container.innerHTML = "";
    if (!connections.length) {
      container.textContent = "No saved Wi-Fi networks.";
      return;
    }
    for (const connection of connections) {
      const row = document.createElement("div");
      row.className = "saved-wifi-network";
      const details = document.createElement("span");
      details.className = "saved-wifi-details";
      details.textContent = `${connection.name}${connection.autoconnect ? "" : " (manual)"}`;
      row.appendChild(details);
      if (connection.active) {
        const active = document.createElement("span");
        active.className = "saved-wifi-active";
        active.textContent = "Connected";
        row.appendChild(active);
      } else {
        const forget = document.createElement("button");
        forget.className = "forget-wifi-network";
        forget.textContent = "Forget";
        forget.onclick = () => forgetSavedWifiNetwork(connection, forget);
        row.appendChild(forget);
      }
      container.appendChild(row);
    }
  } catch (error) {
    container.textContent = "Saved Wi-Fi networks unavailable.";
    showError(error);
  }
}

async function toggleSavedWifiNetworks() {
  savedWifiNetworksExpanded = !savedWifiNetworksExpanded;
  const toggle = document.getElementById("wifi-saved-toggle");
  const container = document.getElementById("wifi-saved-networks");
  toggle.textContent = savedWifiNetworksExpanded ? "Hide Saved Networks" : "Manage Saved Networks";
  toggle.setAttribute("aria-expanded", String(savedWifiNetworksExpanded));
  container.hidden = !savedWifiNetworksExpanded;
  if (savedWifiNetworksExpanded) await loadSavedWifiNetworks();
}

async function forgetSavedWifiNetwork(connection, button) {
  if (!window.confirm(`Forget saved Wi-Fi network “${connection.name}”?`)) return;
  button.disabled = true;
  try {
    await apiJson(`/api/wifi/saved/${encodeURIComponent(connection.uuid)}`, { method: "DELETE" });
    await loadSavedWifiNetworks();
  } catch (error) {
    showError(error);
    button.disabled = false;
  }
}

function selectWifiNetwork(network) {
  selectedWifiNetwork = network;
  document.getElementById("wifi-connect-form").style.display = "block";
  document.getElementById("wifi-selected-network").textContent = `Connect to: ${network.ssid}`;
  document.getElementById("wifi-password").value = "";
  document.getElementById("wifi-password").placeholder = network.security === "Open" ? "No password required" : "Wi-Fi password";
}

function renderWifiNetworks(networks) {
  const container = document.getElementById("wifi-network-list");
  container.innerHTML = "";
  if (!networks.length) {
    container.textContent = "No Wi-Fi networks found.";
    return;
  }
  for (const network of networks) {
    const row = document.createElement("div");
    const button = document.createElement("button");
    button.className = "wifi-network";
    button.textContent = `${network.ssid} — ${network.signal}%${network.active ? " — Connected" : ""}`;
    button.onclick = () => selectWifiNetwork(network);
    const security = document.createElement("span");
    security.textContent = network.security;
    row.append(button, security);
    container.appendChild(row);
  }
}

async function scanWifiNetworks() {
  const scanStatus = document.getElementById("wifi-scan-status");
  scanStatus.textContent = "Scanning…";
  try {
    const data = await apiJson("/api/wifi/scan", { method: "POST" });
    renderWifiNetworks(data.networks);
    scanStatus.textContent = "";
  } catch (error) {
    scanStatus.textContent = error.message || "Wi-Fi scan failed";
    showError(error);
  }
}

document.getElementById("wifi-connect-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (wifiConnectPending) return;
  const connectStatus = document.getElementById("wifi-connect-status");
  const connectButton = event.currentTarget.querySelector("button[type=submit]");
  if (!selectedWifiNetwork) {
    connectStatus.textContent = "Choose a Wi-Fi network first.";
    return;
  }
  const ssid = selectedWifiNetwork.ssid;
  wifiConnectPending = true;
  connectButton.disabled = true;
  connectStatus.textContent = "Connecting…";
  try {
    const result = await apiJson("/api/wifi/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ssid,
        password: document.getElementById("wifi-password").value || null,
      }),
    });
    const address = result.wifi?.ip_address;
    connectStatus.textContent = address
      ? `PiMusic joined ${ssid}. Rejoin your normal Wi-Fi, then open http://${address}:8000.`
      : `PiMusic joined ${ssid}. Rejoin your normal Wi-Fi, then open PiMusic at its normal network address.`;
    await refreshWifiStatus();
  } catch (error) {
    if (error.networkError) {
      connectStatus.textContent = `The connection request was sent. PiMusic may now be joining ${ssid}; rejoin your normal Wi-Fi, then open PiMusic at its normal network address.`;
    } else {
      connectStatus.textContent = error.message || "Unable to connect";
      showError(error);
    }
  } finally {
    wifiConnectPending = false;
    connectButton.disabled = false;
  }
});

async function useBrowserOutput() {
  browserOutputButton.disabled = true;
  try {
    const data = await apiJson("/api/output/browser", { method: "POST" });
    browserLease = data.lease;
    sessionStorage.setItem("pimusic-browser-lease", browserLease);
    await refreshStatus();
  } catch (error) {
    showError(error);
  } finally {
    browserOutputButton.disabled = false;
  }
}

async function usePiOutput() {
  if (!browserLease) return;
  piOutputButton.disabled = true;
  try {
    await apiJson("/api/output/pi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lease: browserLease })
    });
    browserAudio.pause();
    browserAudio.removeAttribute("src");
    browserAudio.load();
    browserSongId = null;
    browserLease = null;
    sessionStorage.removeItem("pimusic-browser-lease");
    await refreshStatus();
  } catch (error) {
    showError(error);
  } finally {
    piOutputButton.disabled = false;
  }
}

async function reportBrowserProgress() {
  if (!browserOutputActive || !browserLease || browserProgressPending) return;
  browserProgressPending = true;
  try {
    await apiJson("/api/browser/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lease: browserLease,
        position: Number.isFinite(browserAudio.currentTime) ? browserAudio.currentTime : 0,
        paused: browserAudio.paused
      })
    });
  } catch (error) {
    if (!error.networkError) showError(error);
  } finally {
    browserProgressPending = false;
  }
}

function startBrowserAudio(player) {
  const ownsLease = Boolean(browserLease);
  browserOutputActive = player.output === "browser" && ownsLease;
  if (player.output !== "browser") {
    outputStatus.textContent = "Playing on Pi audio";
    browserOutputButton.hidden = false;
    piOutputButton.hidden = true;
    browserAudio.controls = false;
    browserAudio.classList.remove("is-active");
    if (browserLease) {
      browserLease = null;
      sessionStorage.removeItem("pimusic-browser-lease");
    }
    if (browserSongId) {
      browserAudio.pause();
      browserAudio.removeAttribute("src");
      browserAudio.load();
      browserSongId = null;
      browserRevision = null;
    }
    return;
  }

  if (!ownsLease) {
    outputStatus.textContent = "Playing in another browser";
    browserOutputButton.hidden = true;
    piOutputButton.hidden = true;
    browserAudio.controls = false;
    browserAudio.classList.remove("is-active");
    return;
  }

  outputStatus.textContent = "Playing in this browser";
  browserOutputButton.hidden = true;
  piOutputButton.hidden = false;
  browserAudio.controls = true;
  browserAudio.classList.add("is-active");
  if (!player.song_id) return;

  if (browserSongId !== player.song_id) {
    browserSongId = player.song_id;
    browserRevision = player.revision;
    browserAudio.dataset.loading = "true";
    browserAudio.src = `/api/browser/stream/${encodeURIComponent(player.song_id)}?lease=${encodeURIComponent(browserLease)}`;
    browserAudio.load();
    browserAudio.onloadedmetadata = () => {
      delete browserAudio.dataset.loading;
      if (Math.abs(browserAudio.currentTime - (player.position || 0)) > 0.5) {
        browserAudio.currentTime = player.position || 0;
      }
      if (!player.paused) browserAudio.play().catch(showError);
    };
  } else if (browserRevision !== player.revision) {
    browserRevision = player.revision;
    if (Math.abs(browserAudio.currentTime - (player.position || 0)) > 0.5) {
      browserAudio.currentTime = player.position || 0;
    }
  }

  if (player.paused) {
    browserAudio.pause();
  } else {
    browserAudio.play().catch(() => {
      outputStatus.textContent = "Tap Play to start browser audio";
    });
  }
}

browserAudio.addEventListener("ended", async () => {
  if (!browserOutputActive || !browserLease) return;
  try {
    await apiJson("/api/browser/ended", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lease: browserLease })
    });
    await refreshStatus();
    await loadQueue();
  } catch (error) {
    showError(error);
  }
});

browserAudio.addEventListener("play", reportBrowserProgress);
browserAudio.addEventListener("pause", () => {
  if (!browserAudio.dataset.loading) reportBrowserProgress();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    reportBrowserProgress();
    refreshAfterTabResume();
  }
});
setInterval(reportBrowserProgress, 5000);

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) {
    return "0:00";
  }

  const minutes = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);

  return `${minutes}:${secs.toString().padStart(2, "0")}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0m";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

async function cycleRepeat() {
  try {
    const data = await apiJson("/api/player/repeat", { method: "POST" });
    updateRepeatButton(data.repeat);
  } catch (error) {
    showError(error);
  }
}

function updateRepeatButton(mode) {
  const button = document.getElementById("repeatButton");
  const label = document.getElementById("repeatLabel");

  button.classList.remove("active");

  if (mode === "all") {
    label.textContent = "All";
    button.classList.add("active");
  } else if (mode === "one") {
    label.textContent = "One";
    button.classList.add("active");
  } else {
    label.textContent = "Off";
  }
}

async function refreshRepeat() {
  const response = await fetch("/api/player/repeat");
  const data = await response.json();

  updateRepeatButton(data.repeat);
}

async function shuffleQueue() {
  try {
    const data = await apiJson("/api/player/shuffle", { method: "POST" });
    updateShuffleButton(data.shuffle);
    await loadQueue();
  } catch (error) {
    showError(error);
  }
}

function updateShuffleButton(enabled) {
  const button = document.getElementById("shuffleButton");

  if (enabled) {
    button.classList.add("active");
  } else {
    button.classList.remove("active");
  }
}

async function refreshShuffle() {
  const response = await fetch("/api/player/shuffle");
  const data = await response.json();

  updateShuffleButton(data.shuffle);
}

async function nextTrack() {
  try {
    await apiJson("/api/player/next", { method: "POST" });
    refreshStatus();
    loadQueue();
  } catch (error) {
    showError(error);
  }
}

async function previousTrack() {
  try {
    await apiJson("/api/player/previous", { method: "POST" });
    refreshStatus();
    loadQueue();
  } catch (error) {
    showError(error);
  }
}

async function clearQueue() {
  const button = document.getElementById("clearQueueButton");
  button.disabled = true;
  try {
    await apiJson("/api/player/queue", { method: "DELETE" });
    updateShuffleButton(false);
    await refreshStatus();
  } catch (error) {
    showError(error);
  } finally {
    await loadQueue();
  }
}

async function loadQueue() {
  if (queueDrag || queueMovePending) return;
  const loadVersion = ++queueLoadVersion;
  const response = await fetch("/api/player/queue");

  if (!response.ok) {
    console.error(await response.text());
    return;
  }

  const data = await response.json();
  if (loadVersion !== queueLoadVersion || queueDrag || queueMovePending) return;
  const container = document.getElementById("queue");
  document.getElementById("clearQueueButton").disabled = !data.queue?.length;

  container.innerHTML = "";
  await refreshShuffle();
  if (loadVersion !== queueLoadVersion || queueDrag || queueMovePending) return;

  if (!data.queue || data.queue.length === 0) {
    container.textContent = "No queue loaded.";
    return;
  }

  for (const item of data.queue) {
    const row = document.createElement("div");
    const queueIds = data.queue.map(entry => entry.queue_id);
    const handle = document.createElement("button");
    handle.className = "queue-drag-handle";
    handle.textContent = "⠿";
    handle.title = "Drag to reorder";
    handle.setAttribute("aria-label", "Drag to reorder track; use Up and Down keys to move");
    handle.onkeydown = async event => {
      const direction = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
      const target = item.position + direction;
      if (!direction || target < 0 || target >= data.queue.length) return;
      event.preventDefault();
      event.stopPropagation();
      await moveQueueItem(item, target, queueIds);
    };
    addQueueDrag(handle, row, item, queueIds);
    row.appendChild(handle);

    let text = `${item.position + 1}. ${item.title || "Unknown title"}`;

    if (item.artist) {
      text += ` — ${item.artist}`;
    }

    if (item.current) {
      text = `▶ ${text}`;
      row.style.fontWeight = "bold";
    }

    const label = document.createElement("span");
    label.className = "queue-label";
    label.textContent = text;
    row.appendChild(label);

    const actions = document.createElement("span");
    actions.className = "queue-actions";
    row.appendChild(actions);

    const removeButton = document.createElement("button");
    removeButton.className = "queue-remove";
    removeButton.textContent = "×";
    removeButton.title = "Remove from queue";
    removeButton.setAttribute("aria-label", `Remove ${item.title || "track"} from queue`);
    removeButton.onclick = async (event) => {
      event.stopPropagation();
      removeButton.disabled = true;
      try {
        await apiJson(
          `/api/player/queue/${item.position}?queue_id=${encodeURIComponent(item.queue_id)}`,
          { method: "DELETE" }
        );
        await refreshStatus();
      } catch (error) {
        showError(error);
      } finally {
        removeButton.disabled = false;
        await loadQueue();
      }
    };
    actions.appendChild(removeButton);
    row.style.padding = "4px 0";
    row.style.cursor = "pointer";

    row.onclick = async () => {
      try {
        await apiJson(`/api/player/queue/${item.position}/play`, { method: "POST" });
        await refreshStatus();
        await loadQueue();
      } catch (error) {
        showError(error);
      }
    };

    container.appendChild(row);
  }
}

async function scanBluetoothDevices() {
  const status = document.getElementById("bluetooth-scan-status");
  const button = document.getElementById("bluetooth-scan-button");
  if (button.disabled) return;

  button.disabled = true;
  status.textContent = "Scanning for speakers (up to 30 seconds)...";

  try {
    const data = await apiJson(
      "/api/bluetooth/scan",
      { method: "POST" },
      BLUETOOTH_SCAN_TIMEOUT_MS
    );
    renderBluetoothScanDevices(data.devices || []);
    status.textContent = data.devices?.length
      ? "Scan complete."
      : "No speakers found. Keep the speaker close and in pairing mode, then try again.";

  } catch (error) {
    status.textContent = error.message || "Scan failed";
    showError(error);
  } finally {
    button.disabled = false;
  }
}

function clearBluetoothScanResults() {
  document.getElementById("bluetooth-device-list").innerHTML = "";
}

async function toggleSavedBluetoothDevices() {
  savedBluetoothDevicesExpanded = !savedBluetoothDevicesExpanded;
  const button = document.getElementById("bluetooth-saved-toggle");
  const container = document.getElementById("bluetooth-saved-devices");
  button.setAttribute("aria-expanded", String(savedBluetoothDevicesExpanded));
  button.textContent = savedBluetoothDevicesExpanded ? "Hide Saved Speakers" : "Manage Saved Speakers";
  container.hidden = !savedBluetoothDevicesExpanded;
  if (savedBluetoothDevicesExpanded) await loadSavedBluetoothDevices();
}

async function loadSavedBluetoothDevices() {
  const container = document.getElementById("bluetooth-saved-devices");
  container.textContent = "Loading saved speakers...";
  try {
    const data = await apiJson("/api/bluetooth/saved");
    renderSavedBluetoothDevices(data.devices || []);
  } catch (error) {
    container.textContent = error.message;
  }
}

function renderSavedBluetoothDevices(devices) {
  const container = document.getElementById("bluetooth-saved-devices");
  container.innerHTML = "";
  if (!devices.length) {
    container.textContent = "No saved Bluetooth speakers.";
    return;
  }
  for (const device of devices) {
    const row = document.createElement("div");
    row.className = "saved-bluetooth-device";
    const details = document.createElement("div");
    details.className = "saved-bluetooth-details";
    const name = document.createElement("div");
    name.textContent = `${device.name} (${device.address})`;
    const state = document.createElement("div");
    state.className = "saved-bluetooth-state";
    const labels = [];
    if (device.connected) labels.push("Connected");
    if (device.trusted) labels.push("Trusted");
    if (device.preferred) labels.push("Reconnects at boot");
    state.textContent = labels.length ? labels.join(" · ") : "Paired";
    details.append(name, state);
    row.appendChild(details);
    const useButton = document.createElement("button");
    useButton.className = "use-bluetooth-device";
    useButton.textContent = device.connected ? "Use" : "Connect";
    useButton.onclick = async () => {
      await connectBluetoothDevice(device.address);
    };
    row.appendChild(useButton);
    if (device.connected) {
      const outputLevel = document.createElement("div");
      outputLevel.className = "bluetooth-output-level";
      const label = document.createElement("label");
      label.textContent = "Speaker output level";
      outputLevel.appendChild(label);
      if (typeof device.output_volume === "number") {
        const value = document.createElement("span");
        value.className = "bluetooth-output-value";
        value.textContent = `${device.output_volume}%`;
        label.appendChild(value);
        const slider = document.createElement("input");
        slider.type = "range";
        slider.min = "0";
        slider.max = "100";
        slider.step = "1";
        slider.value = String(device.output_volume);
        slider.setAttribute("aria-label", `Speaker output level for ${device.name}`);
        slider.onchange = async () => {
          const volume = Number(slider.value);
          if (volume === 100 && !window.confirm("Setting speaker output to 100% may make playback much louder. Continue?")) {
            slider.value = String(device.output_volume);
            return;
          }
          await setBluetoothOutputVolume(device.address, volume);
        };
        const maximum = document.createElement("button");
        maximum.type = "button";
        maximum.textContent = "Set to 100%";
        maximum.onclick = async () => {
          if (!window.confirm("Setting speaker output to 100% may make playback much louder. Continue?")) return;
          await setBluetoothOutputVolume(device.address, 100);
        };
        outputLevel.append(slider, maximum);
      } else {
        const unavailable = document.createElement("span");
        unavailable.className = "bluetooth-output-unavailable";
        unavailable.textContent = "Unavailable while the audio output is starting.";
        outputLevel.appendChild(unavailable);
      }
      row.appendChild(outputLevel);
    }
    if (!device.connected) {
      const forget = document.createElement("button");
      forget.className = "forget-bluetooth-device";
      forget.textContent = "Forget";
      forget.onclick = async () => {
        const warning = device.preferred
          ? `Forget ${device.name}? It will also stop reconnecting automatically at boot.`
          : `Forget ${device.name}?`;
        if (!window.confirm(warning)) return;
        forget.disabled = true;
        try {
          await apiJson(`/api/bluetooth/saved/${encodeURIComponent(device.address)}`, { method: "DELETE" });
          await loadSavedBluetoothDevices();
        } catch (error) {
          showError(error);
          forget.disabled = false;
        }
      };
      row.appendChild(forget);
    }
    container.appendChild(row);
  }
}

function renderBluetoothScanDevices(devices) {
  const container = document.getElementById("bluetooth-device-list");
  container.innerHTML = "";
  const newDevices = devices.filter((device) => !device.paired);

  if (newDevices.length === 0) {
    container.textContent = "No new speakers found. Use Manage Saved Speakers for paired speakers.";
    return;
  }

  for (const device of newDevices) {
    const row = document.createElement("div");

    const label = document.createElement("span");

    label.textContent =
      `${device.name} (${device.address})` +
      (device.connected ? " — Connected" : "");

    const button = document.createElement("button");

    button.textContent = "Pair";
    button.onclick = async () => {
      await pairBluetoothDevice(device.address);
    };

    row.appendChild(label);
    row.appendChild(document.createTextNode(" "));
    row.appendChild(button);

    container.appendChild(row);
  }
}

async function pairBluetoothDevice(address) {
  try {
    await apiJson("/api/bluetooth/pair-device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address })
    });
    await refreshBluetoothStatus();
    clearBluetoothScanResults();
    if (savedBluetoothDevicesExpanded) await loadSavedBluetoothDevices();
  } catch (error) {
    showError(error);
  }
}

async function connectBluetoothDevice(address) {
  try {
    await apiJson("/api/bluetooth/connect-device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address })
    });
    await refreshBluetoothStatus();
    if (savedBluetoothDevicesExpanded) await loadSavedBluetoothDevices();
  } catch (error) {
    showError(error);
  }
}

async function setBluetoothOutputVolume(address, volume) {
  try {
    await apiJson(`/api/bluetooth/saved/${encodeURIComponent(address)}/output-volume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume })
    });
    await loadSavedBluetoothDevices();
  } catch (error) {
    showError(error);
    await loadSavedBluetoothDevices();
  }
}

async function refreshBluetoothStatus() {
  const response = await fetch("/api/bluetooth/status");
  const data = await response.json();

  document.getElementById("bluetooth-name").textContent =
    data.name || data.address || "Unknown";

  document.getElementById("bluetooth-status").textContent =
    data.connected ? "Connected" : "Disconnected";
}

async function loadPlaylists() {
  const response = await fetch("/api/library/playlists");
  const data = await response.json();

  const container = document.getElementById("playlists");
  container.innerHTML = "";

  for (const playlist of data.playlists) {
    const item = document.createElement("div");

    item.textContent =
      `${playlist.name} (${playlist.songCount} songs)`;

    item.style.padding = "8px 0";
    item.style.cursor = "pointer";

    item.onclick = () => loadPlaylist(playlist.id);

    container.appendChild(item);
  }
}


async function loadPlaylist(playlistId) {
  const response = await fetch(`/api/library/playlists/${playlistId}`);
  const playlist = await response.json();

  const container = document.getElementById("playlists");
  container.innerHTML = "";

  const back = document.createElement("button");
  back.textContent = "← Playlists";
  back.onclick = loadPlaylists;
  container.appendChild(back);

  const heading = document.createElement("h2");
  heading.textContent = playlist.name;
  container.appendChild(heading);

  appendCollectionActions(container, "playlist", playlistId, `/api/player/play-playlist/${playlistId}`);

  for (const song of playlist.songs) {
    appendSongRow(container, `${song.title} \u2014 ${song.artist || ""}`, song.id);
  }
}

async function searchLibrary() {
  const query = document.getElementById("searchBox").value.trim();
  const container = document.getElementById("searchResults");

  if (!query) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = "Searching...";

  let data;
  try {
    data = await apiJson(`/api/library/search?q=${encodeURIComponent(query)}`);
  } catch (error) {
    container.textContent = error.message;
    showError(error);
    return;
  }

  container.innerHTML = "";

  if (data.artists.length) {
    const heading = document.createElement("h3");
    heading.textContent = "Artists";
    container.appendChild(heading);

    for (const artist of data.artists) {
      const item = document.createElement("div");
      item.textContent = artist.name;
      item.style.padding = "6px 0";
      item.style.cursor = "pointer";

      item.onclick = () => loadArtist(artist.id);

      container.appendChild(item);
    }
  }

  if (data.albums.length) {
    const heading = document.createElement("h3");
    heading.textContent = "Albums";
    container.appendChild(heading);

    for (const album of data.albums) {
      const item = document.createElement("div");

      item.textContent =
        `${album.name} — ${album.artist || ""}`;

      item.style.padding = "6px 0";
      item.style.cursor = "pointer";

      item.onclick = () => loadAlbum(album.id, null);

      container.appendChild(item);
    }
  }

  if (data.songs.length) {
    const heading = document.createElement("h3");
    heading.textContent = "Songs";
    container.appendChild(heading);

    for (const song of data.songs) {
      appendSongRow(container, `${song.title} \u2014 ${song.artist || ""}`, song.id);
    }
  }
}

async function loadArtist(artistId) {
  const response = await fetch(`/api/library/artists/${artistId}`);
  const artist = await response.json();

  const container = document.getElementById("searchResults");
  container.innerHTML = "";

  const back = document.createElement("button");
  back.textContent = "← Search Results";
  back.onclick = () => searchLibrary();
  container.appendChild(back);

  const heading = document.createElement("h2");
  heading.textContent = artist.name;
  container.appendChild(heading);

  for (const album of artist.albums) {
    const item = document.createElement("div");

    let details = album.name;

    if (album.year) {
      details += ` (${album.year})`;
    }

    details += ` — ${album.songCount} song${album.songCount === 1 ? "" : "s"}`;

    item.textContent = details;
    item.style.padding = "8px 0";
    item.style.cursor = "pointer";

    item.addEventListener("click", () => {
      loadAlbum(album.id, artist.id);
    });

    container.appendChild(item);
  }
}

async function loadAlbum(albumId, artistId) {
  const response = await fetch(`/api/library/albums/${albumId}`);
  const album = await response.json();

  const container = document.getElementById("searchResults");
  container.innerHTML = "";

  const back = document.createElement("button");
  back.textContent = "← Search Results";
  back.onclick = () => searchLibrary();
  container.appendChild(back);

  const heading = document.createElement("h2");
  heading.textContent = album.name;
  container.appendChild(heading);

  appendCollectionActions(container, "album", albumId, `/api/player/play-album/${albumId}`);

  const subheading = document.createElement("div");
  subheading.textContent = album.artist || "";
  container.appendChild(subheading);

  for (const song of album.songs) {
    appendSongRow(container, `${song.track ?? ""}. ${song.title}`, song.id);
  }
}

    async function refreshStatus() {
      if (statusRefreshPending) return false;
      statusRefreshPending = true;
      let data;
      try {
        data = await apiJson("/api/status");
      } catch {
        return false;
      } finally {
        statusRefreshPending = false;
      }
      const player = data.player;
      startBrowserAudio(player);

      if (player.coverArt) {
        const coverUrl = `/api/cover/${encodeURIComponent(player.coverArt)}`;
        if (coverArt.getAttribute("src") !== coverUrl) {
          coverArt.src = coverUrl;
        }
        coverArt.style.display = "block";
      } else {
        coverArt.removeAttribute("src");
        coverArt.style.display = "none";
      }

     if (player.paused || player.idle) {
       playPauseButton.textContent = "▶";
       playPauseButton.title = "Play";
     } else {
       playPauseButton.textContent = "⏸";
       playPauseButton.title = "Pause";
     }

      title.textContent = player.title || "Nothing playing";
      artist.textContent = player.artist || "";
      album.textContent = player.album || "";
      const queue = data.queue || {};
      upNext.textContent = queue.up_next
        ? `Up next: ${queue.up_next.title || "Unknown title"}${queue.up_next.artist ? ` — ${queue.up_next.artist}` : ""}`
        : "Up next: End of queue";
      queueRuntime.textContent = queue.total_runtime
        ? `${queue.tracks_remaining || 0} track${queue.tracks_remaining === 1 ? "" : "s"} after this · ${formatDuration(queue.remaining_runtime)} left · ${formatDuration(queue.total_runtime)} total`
        : "";

      const currentSongId = player.song_id || null;
      
      if (currentSongId !== lastCurrentSongId) {
          lastCurrentSongId = currentSongId;
          loadQueue();
      }

      const duration = player.idle ? 0 : (player.duration || 0);
      const position = player.idle ? 0 : (player.position || 0);
      seek.max = duration;
      seek.value = position;
      seek.disabled = duration <= 0;
      elapsedTime.textContent = formatTime(position);
      totalTime.textContent = formatTime(duration);

      if (player.output === "browser") {
        volume.value = Math.round(browserAudio.volume * 100);
      } else if (player.volume !== null) {
        volume.value = player.volume;
      }
      return true;
    }

    async function refreshAfterTabResume() {
      if (resumeRefreshPending) return;
      resumeRefreshPending = true;
      playPauseButton.disabled = true;
      outputStatus.textContent = "Reconnecting to PiMusic…";
      try {
        const refreshed = await refreshStatus();
        if (!refreshed) {
          showError(new Error("PiMusic is still reconnecting. Try Play again once the connection returns."));
        }
      } finally {
        playPauseButton.disabled = false;
        resumeRefreshPending = false;
      }
    }

    window.addEventListener("pageshow", (event) => {
      if (event.persisted) refreshAfterTabResume();
    });

    async function play() {
      try {
        await apiJson("/api/player/play", { method: "POST" });
        refreshStatus();
      } catch (error) {
        showError(error);
      }
    }

    async function pause() {
      try {
        await apiJson("/api/player/pause", { method: "POST" });
        refreshStatus();
      } catch (error) {
        showError(error);
      }
    }

    seek.addEventListener("change", async () => {
      try {
        if (browserOutputActive) browserAudio.currentTime = Number(seek.value);
        await apiJson("/api/player/seek", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ position: Number(seek.value) })
        });
      } catch (error) {
        showError(error);
      }
    });

    volume.addEventListener("change", async () => {
      try {
        if (browserOutputActive) {
          browserAudio.volume = Number(volume.value) / 100;
          return;
        }
        await apiJson("/api/player/volume", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ volume: Number(volume.value) })
        });
      } catch (error) {
        showError(error);
      }
    });

    refreshStatus();
    setInterval(refreshStatus, 1000);

    loadPlaylists();

    refreshBluetoothStatus();
    refreshWifiStatus();
    refreshWifiSetupStatus();
    setInterval(refreshWifiSetupStatus, 10000);
    refreshSystemStatus();
    loadNavidromeConfig();
    refreshRepeat();
    refreshShuffle();

    loadQueue();

