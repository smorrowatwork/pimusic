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
hotspot immediately. Verify the normal Wi-Fi path first, then enable it:

```bash
sudo systemctl start pimusic-hotspot-fallback.service
sudo systemctl status pimusic-hotspot-fallback.service --no-pager
```

To test fallback, temporarily turn off the usual access point or take the Pi out
of range. Join `pimusic-setup` from a phone or tablet, then open
`http://10.42.0.1:8000`. When a saved network returns, the service checks again
within 30 seconds and reconnects the Pi to it.

To stop or remove the feature:

```bash
sudo systemctl disable --now pimusic-hotspot-fallback.service
sudo rm /etc/systemd/system/pimusic-hotspot-fallback.service
sudo rm /usr/local/lib/pimusic/pimusic-hotspot-fallback
sudo nmcli connection delete pimusic-setup
sudo systemctl daemon-reload
```
