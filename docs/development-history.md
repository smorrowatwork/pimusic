# Development history and durable decisions

This is a concise record of verified project context, not a debugging transcript. See [README.md](../README.md) for current installation and operator procedures.

## Established implementation and lessons

- PiMusic's working core use case is Navidrome to MPV to a local, commonly Bluetooth, speaker.
- A persistent idle MPV process and its IPC socket were chosen to keep controls responsive and preserve the live queue across web-backend restarts. Application state supplements MPV when MPV restarts empty.
- Headless user services need `loginctl enable-linger`; MPV, PipeWire, and the backend need to share the intended user session.
- Bluetooth pairing/connection is handled with `bluetoothctl`, while PipeWire output selection is handled with `wpctl`. The project deliberately does not depend on `pactl` for this.
- The Pi Zero 2 W's shared Wi-Fi/Bluetooth radio and limited resources matter in deployment. Check power throttling and Wi-Fi power saving when Bluetooth is unreliable.
- An optional NetworkManager hotspot-fallback service is already included. It attempts normal saved Wi-Fi before activating `pimusic-setup`; the web UI can explicitly request or exit that existing setup mode through one root-owned constrained helper. This is not yet the full interactive provisioning system described below.
- When `pimusic-setup` is active, the fallback service must not repeatedly reactivate it or attempt a competing Wi-Fi activation: either action disconnects setup clients. The web connection flow pauses fallback during its explicit NetworkManager connection attempt, then resumes it once the attempt has completed.
- The setup-hotspot flow has been tested end-to-end. Submitting normal Wi-Fi credentials necessarily removes the setup access point, so the browser must treat a post-submission network loss as expected and direct the user back to normal Wi-Fi. NetworkManager failures are summarized into safe authentication, authorization, availability, or timeout messages; raw command output is not sent to the browser.
- The constrained hotspot helper now provides a lightweight shared provisioning status: `READY`, `TRYING_WIFI`, `PROVISIONING`, or `CONNECTING_WIFI`. It is derived from NetworkManager and a root-owned runtime marker, and is consumed by the web UI. This is a foundation for the later physical button, not a complete event-driven state machine.
- Browser listening is an explicit, leased output mode. MPV remains paused while one browser renders the same queue through a PiMusic stream proxy; progress is mirrored into MPV for queue operations and handoff back to the Pi. Browser volume is local, and an expired five-minute browser lease pauses playback; the window accommodates background-tab timer throttling.
- The optional Bluetooth reconnect installer saves a selected speaker through one root-owned MAC-validation helper and retries that trusted device at boot. The web service has no broader Bluetooth or root permission.
- Navidrome server changes take a server address, username, and password from the local web UI; the Pi generates the salt and Subsonic token, validates them, and atomically saves only the derived credentials in the private PiMusic environment file. A successful change is applied only after the active queue has been cleared.
- The optional `pimusic-gpio.service` runs as the PiMusic user and owns the provision button plus READY/SETUP LEDs. It consumes the same constrained provisioning-status helper as the web UI, and a three-second GPIO17 hold invokes only that helper's setup-start action.
- Bluetooth speaker discovery is serialized in the backend and runs for up to 30 seconds; the UI disables its scan action while that request is active. This avoids overlapping BlueZ discovery requests, which can leave the adapter unable to scan until reset.
- The development Pi has a retained USB Ethernet gadget recovery path. It is useful when normal Wi-Fi is unavailable, but it is a local development-machine configuration rather than a PiMusic installation feature.
- Full VS Code Remote plus Codex workloads on the Pi have exhausted RAM/zram and made it nearly unresponsive. Prefer development on a PC and use the Pi mainly for deployment and test; keep runtime dependencies lightweight.

## Known project decisions / context

- PiMusic should behave as a portable appliance.
- NetworkManager is the preferred Wi-Fi mechanism, rather than a separate hand-maintained hostapd/dnsmasq stack, unless a technical reason requires otherwise.
- Normal FastAPI/web code should remain unprivileged. Privileged Wi-Fi/network operations should move behind a small, constrained helper/service.

## Backlog

This is the single list of unimplemented PiMusic features and improvements discussed so far. Entries are intentionally not commitments or a delivery order; they record scope and constraints for later prioritization.

### Complete shared Wi-Fi provisioning

- Evolve the current lightweight provisioning status into a durable, event-driven state flow shared by automatic saved-network fallback, the web UI, and future physical controls. It should cover `BOOTING`, `TRYING_WIFI`, `READY`, `PROVISIONING`, and `CONNECTING_WIFI` without competing NetworkManager activation attempts.
- Provide a phone-friendly captive-portal-style setup experience when the temporary `PiMusic-Setup` access point is active. Normal-network discovery is available through the device hostname's mDNS name (for example, `pimusic.local`).
- Consider QR or NFC setup assistance only after the core provisioning flow is reliable; neither is a current installation requirement.

### Extend GPIO controls and indicators

- Evaluate a longer GPIO17 hold for forgetting saved Wi-Fi only after defining its recovery and confirmation behavior. The existing three-second hold exclusively requests provisioning.

### Make queue ownership backend-authoritative

- Replace the persistent MPV playlist as the queue execution source with a backend-owned, persisted queue model. MPV and browser playback should become renderers of that model, preserving the current one-renderer-at-a-time behavior while making output handoff and future library changes simpler.

### Make all card actions phone friendly

- Review every browser-card action for phone use. Apply the Wi-Fi card pattern where appropriate: comfortably sized tap targets, clear separation between adjacent actions, and single-column wrapping on narrow screens. Preserve useful compact layouts on larger screens and do not change action behavior as part of this visual pass.

## Important boundary

The backlog is planned direction, not evidence of implementation. In particular, the current backend directly invokes `nmcli` for Wi-Fi operations, the repository's fallback service is a separate loop rather than the proposed shared state machine, and no GPIO code or hardware wiring configuration is present.
