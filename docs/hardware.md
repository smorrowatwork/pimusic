# Hardware and deployment assumptions

## Current target and tested assumptions

- Raspberry Pi Zero 2 W on a headless, Debian-based system (the README targets 64-bit Debian 13 / Raspberry Pi OS).
- Wi-Fi for normal network access and Bluetooth speaker output; the Pi Zero 2 W shares the Wi-Fi/Bluetooth radio.
- PipeWire, WirePlumber, BlueZ, NetworkManager, and MPV are expected runtime components. USB, HDMI, and other local PipeWire outputs are also supported by the application.
- A display manager is unnecessary for the intended appliance deployment. Keep SSH available for administration.

### Operational lessons already captured in the README

- Use systemd user-service lingering so the MPV, PipeWire, and web services have a persistent user session at boot.
- Select Bluetooth audio through PipeWire (`wpctl`), not `pactl`. Bluetooth sinks use names beginning `bluez_output.`.
- PipeWire RTKit warnings are not necessarily failures; investigate them when audio is actually failing.
- If Bluetooth is unreliable, check power throttling first. Wi-Fi power saving on the active NetworkManager profile can be disabled when needed; do not hard-code a profile name.
- The optional fallback hotspot is NetworkManager shared IPv4 and normally presents the Pi at `10.42.0.1`. Its installer keeps the hotspot password in a root-readable NetworkManager connection profile, not in the repository.

See [README.md](../README.md) for installation, audio verification, Wi-Fi power-saving commands, hotspot testing, and troubleshooting.

## Development USB recovery

The development Pi may be configured for USB Ethernet gadget recovery on its micro-USB **data** port (the port closest to HDMI). This is a development-only convenience, not an installer requirement: it reserves that port for gadget mode and therefore excludes normal USB peripherals. On Windows, the Raspberry Pi USB Ethernet/RNDIS driver and Internet Connection Sharing can provide a temporary recovery network if ordinary Wi-Fi is unavailable. Preserve the device's normal SSH host-key verification when using that path, and do not add its private network or credentials to the repository.

## Planned physical controls — not implemented

| Function | GPIO / physical pin | Electrical assumption | Intended behavior |
| --- | --- | --- | --- |
| Provisioning button | GPIO17 / pin 11 | Momentary switch to GND; internal pull-up | Hold about 3 seconds to enter provisioning; short press does nothing. A longer reset/forget-network hold is only a possibility. |
| READY LED | GPIO22 / pin 15 | About 330-ohm series resistor | Off while booting/unavailable; solid when operational; degraded blink is possible later. |
| SETUP LED | GPIO27 / pin 13 | About 330-ohm series resistor | Off normally; slow blink during provisioning AP; fast blink during Wi-Fi connection; a brief success indication is possible. |

No GPIO code, LED wiring configuration, or button-triggered provisioning is currently in the repository. Treat the table as a hardware/design plan, not deployment instructions; the implementation work is tracked in the [project backlog](development-history.md#backlog).
