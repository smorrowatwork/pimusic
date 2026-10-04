---
name: pimusic-remote-pi
description: Deploy selected PiMusic files from the local Windows repository to its Raspberry Pi and run lightweight remote checks, service actions, or log inspection. Use for requests to deploy, sync, test, restart, inspect, compare, or check PiMusic on the Pi; not for general local development.
---

# PiMusic remote Pi workflow

Use this skill for PiMusic work involving the Raspberry Pi deployment/test target. The local Windows Git working tree is authoritative; the Pi is a constrained runtime target, never the source of truth unless the user explicitly says otherwise.

## Target and boundaries

- **LOCAL:** this Windows repository. Edit and validate source here first.
- **REMOTE:** `stevenm@mypizero2:/home/stevenm/pimusic`. Use it only for deployment, diagnosis, and lightweight runtime testing.
- Use normal SSH configuration and key-based authentication. Never add SSH configuration or keys, embed a password, copy credentials, or disable host-key checking.
- Do not deploy, restart, or otherwise contact the Pi until the user asks for that action. This skill's helpers do not perform automatic deployment.
- Read this repository's `AGENTS.md`, `README.md`, and relevant `docs/` before changing or restarting behavior. They are the project-specific source of truth.

## Default workflow

1. Inspect the relevant local implementation and `git diff`; make edits locally.
2. Run practical local validation before deployment.
3. Identify the smallest affected set of repository-relative files. Do not copy `.git`, `.venv`, caches, editor/Codex state, local secrets, or temporary files.
4. Check connectivity with a short SSH command before assuming the Pi is reachable.
5. Use `scripts/deploy-files.ps1` for explicit-file deployment. It uses `scp`, which is available in this Windows environment; do not assume `rsync` is available. It never deletes remote files.
6. Restart only the affected service after confirming its current name in the project documentation or remote status. Use `scripts/pimusic-restart.ps1` only for an explicitly requested restart.
7. Inspect status and relevant recent logs, then run a lightweight health check appropriate to the change.
8. Report copied files, restarted services, status/log findings, and remaining manual testing.

## Remote-operation safety

- Favor short, serial commands: service status/logs, `ps`, `free`, `uptime`, `ip`, read-only `nmcli`, port checks, and small HTTP checks.
- Do not run Codex, VS Code Server, language servers, indexing, broad recursive scans, or memory-heavy builds on the Pi. The Pi Zero 2 W has previously become nearly inaccessible after RAM/zram exhaustion.
- Do not reboot, power off, change SSH/firewall/NetworkManager configuration, install/remove packages, overwrite `/etc`, modify partitions/filesystems, or run broad root commands unless the user explicitly requests and authorizes the operation.
- Use `sudo` only when necessary. Standard PiMusic web and MPV services are documented as non-root systemd user services.
- Do not make remote-only source edits. If an exception is explicitly requested for troubleshooting, state it first and reproduce or retrieve the change in the local repository afterward.

## File conflict handling

`deploy-files.ps1` transfers only explicitly named regular files and rejects known development-only paths. It compares local and remote SHA-256 values when the remote file exists. If they differ, inspect the difference before using its explicit `-AllowOverwrite` switch. Do not use that switch to overwrite remote-only configuration or data blindly.

## Helpers

All helper commands may override `-Host`, `-User`, or `-RemoteRoot` for an explicitly requested target change.

- `scripts/remote-command.ps1 -Command 'uptime'` runs and prints a transparent SSH command.
- `scripts/deploy-files.ps1 -File app.py,static/app.js` previews selected files. Add `-AllowOverwrite` only after inspecting a reported remote difference.
- `scripts/pimusic-status.ps1` retrieves user-service status plus lightweight resource information.
- `scripts/pimusic-restart.ps1 -Service pimusic-web.service` restarts one explicitly named user service and reports status.
- `scripts/pimusic-logs.ps1 -Service pimusic-web.service` retrieves recent user-service logs.

The documented services are `pimusic-web.service` and `pimusic-mpv.service`; confirm them before use because user-unit installation is described in the README rather than tracked as complete unit files in this repository.
