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
- The development Pi has a retained USB Ethernet gadget recovery path. It is useful when normal Wi-Fi is unavailable, but it is a local development-machine configuration rather than a PiMusic installation feature.
- Full VS Code Remote plus Codex workloads on the Pi have exhausted RAM/zram and made it nearly unresponsive. Prefer development on a PC and use the Pi mainly for deployment and test; keep runtime dependencies lightweight.

## Known project decisions / context

- PiMusic should behave as a portable appliance.
- Wi-Fi provisioning is the next major feature area. NetworkManager is the preferred mechanism, rather than a separate hand-maintained hostapd/dnsmasq stack, unless a technical reason requires otherwise.
- Normal FastAPI/web code should remain unprivileged. Privileged Wi-Fi/network operations should move behind a small, constrained helper/service.
- Provisioning must eventually be entered by automatic failure to join known Wi-Fi, the GPIO setup button, or the web interface. All three should use the same underlying state/provisioning logic.
- Intended high-level states: `BOOTING`, `TRYING_WIFI`, `READY`, `PROVISIONING`, and `CONNECTING_WIFI`.
- Intended flow: try known Wi-Fi; if unavailable, create temporary `PiMusic-Setup` AP; phone joins it; a captive-portal-style UI scans/selects SSID and accepts a password; NetworkManager saves/connects; normal mode resumes.
- `pimusic.local` through mDNS/Avahi is planned. NFC/QR setup assistance may follow later but is not immediate priority.

## Future consideration: browser playback

- Browser playback may be useful for listening through the controlling phone or computer's local speakers, headphones, or Bluetooth output. Treat it as an explicit listening mode, not an additional simultaneous player: switching modes must make the active player clear and prevent duplicate audio or diverging queue positions.
- The Pi should continue to proxy any browser stream so Navidrome credentials remain server-side. This is a lower priority than provisioning state, and its queue ownership, mobile-browser behavior, and Pi bandwidth implications need design before implementation.

## Future consideration: Navidrome server configuration

- Provide a deliberate way to configure or switch to another Navidrome/Subsonic server. The browser must never receive server credentials; any configuration workflow should validate connectivity on the Pi, store sensitive values in the existing private configuration location, and clearly handle the effect on the active queue and playback state.

## Important boundary

The decisions in the previous section are planned direction, not evidence of implementation. In particular, the current backend directly invokes `nmcli` for Wi-Fi operations, and the repository's fallback service is a separate loop rather than the proposed shared state machine.
