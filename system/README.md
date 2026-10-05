# PiMusic hotspot fallback

The fallback service first tries `netplan-wlan0-morrow`, then asks NetworkManager
to activate another suitable saved Wi-Fi profile. If none connects, it starts the
`pimusic-setup` hotspot. NetworkManager's shared IPv4 mode normally gives the Pi
the address `10.42.0.1`, so the PiMusic page is usually available at
`http://10.42.0.1:8000` while the hotspot is active.

Install on the Pi from the project directory:

```bash
chmod +x system/install-hotspot-fallback.sh
sudo ./system/install-hotspot-fallback.sh
```

The installer securely prompts for the hotspot password and writes it only to
NetworkManager's root-readable connection profile. It does not activate the
hotspot immediately. It also installs a root-owned, allow-listed control helper
for the PiMusic web service; this helper may only report setup status or enter/
exit explicitly requested setup mode. Verify the normal Wi-Fi path first, then
enable it:

```bash
sudo systemctl start pimusic-hotspot-fallback.service
sudo systemctl status pimusic-hotspot-fallback.service --no-pager
```

To test fallback, temporarily turn off the usual access point or take the Pi out
of range. Join `pimusic-setup` from a phone or tablet, then open
`http://10.42.0.1:8000`. While the hotspot is active, use the Wi-Fi card to
choose and connect a saved or newly scanned network; this avoids interrupting
connected setup clients with repeated background reconnect attempts.

Submitting normal Wi-Fi credentials intentionally removes the setup hotspot.
The browser may therefore lose its connection before it receives the successful
response. Rejoin the normal Wi-Fi network, then open PiMusic at its hostname or
current DHCP address. Confirmed connection failures remain on the page as safe,
actionable messages; passwords and raw NetworkManager output are never shown.

## Web setup controls

After installing this version of the fallback service, the PiMusic Wi-Fi card
shows **Start Setup Hotspot** and **Exit Setup Mode**. Starting setup mode writes
a runtime-only request and restarts the fallback service, which immediately
activates `pimusic-setup` even if a normal Wi-Fi network is currently reachable.
Exiting removes that request and restarts the service so it immediately tries
saved Wi-Fi again. If no saved network is available, normal fallback behavior
continues and the hotspot may remain active.

While the hotspot is active, the fallback service leaves it in place rather
than repeatedly interrupting connected setup clients to retry saved Wi-Fi.
The web connection flow briefly pauses the fallback service while NetworkManager
joins the selected Wi-Fi network, then resumes normal fallback afterward.

The Wi-Fi card reports the shared provisioning status exposed by the constrained
helper: `READY` when normal Wi-Fi is connected, `TRYING_WIFI` while fallback is
looking for a saved network, `PROVISIONING` while the hotspot is active or being
requested, and `CONNECTING_WIFI` during an explicit web connection attempt.

To stop or remove the feature:

```bash
sudo systemctl disable --now pimusic-hotspot-fallback.service
sudo rm /etc/systemd/system/pimusic-hotspot-fallback.service
sudo rm /usr/local/lib/pimusic/pimusic-hotspot-fallback
sudo nmcli connection delete pimusic-setup
sudo systemctl daemon-reload
```
