# sentry-agent-pc

The Windows camera agent for **Chipmo Sentry**. It runs on a PC inside the store, finds the cameras on the
local network, pairs the store to the cloud with a 6-digit code, and relays each camera's stream up to the
AI host — with **zero new hardware** and a one-click installer.

Python 3.11 · CustomTkinter (desktop GUI) · ONVIF · OpenCV · ffmpeg · PyInstaller (`--onedir` .exe) · Apache-2.0

**Current release: v0.7.2** — the **offline LAN live view** now pulls each camera's **low-res sub-stream**
(falling back to the main stream), so the grid stays smooth even on a PC that's also running the AI workers.
Builds on v0.7.1's responsive dark grid, colour-coded status badges, double-click-to-focus, and fixed tile sizing.

---

## What it does

- **Discover cameras** — one-button scan combining ONVIF WS-Discovery (across all network interfaces, so
  multi-homed PCs work) with a LAN port-554 sweep and an RTSP-path resolver. Results show in a table with a
  password-reveal toggle and a "hide already-registered" filter. Handles **H.264 and H.265** cameras
  (Hikvision / UNV / Dahua / TP-Link / Reolink + generic ONVIF templates).
- **Probe** — a fast, reliable `ffmpeg` RTSP probe reads the real codec + resolution and detects auth
  errors *without* tripping camera account lockouts.
- **Pair** — enter a 6-digit store code (generated in the web app's "Компьютер холбох" flow) → the agent
  redeems it at `/api/v1/agents/pair`, receives a scoped **agent JWT**, and stores it in an encrypted state
  file (Fernet key derived from the Windows machine GUID, so it survives reboots and network changes).
  Paired cameras register via the agent-scoped `/api/v1/agent/cameras` endpoints.
- **Relay (push)** — for each camera, a managed `ffmpeg` relay pushes the LAN RTSP stream to the cloud
  MediaMTX (`-c copy`; **H.265 → H.264** transcode via `libx264` for browser compatibility), with
  exponential-backoff auto-restart. The agent reads publish mode + the relay target from the backend's
  `/api/v1/agent/stream-config`, so the same binary works in LAN-pull or cloud-push topology.
- **Offline LAN live view (v0.7.0)** — decode cameras directly over RTSP with OpenCV and show them in a
  responsive in-app grid. No internet, no MediaMTX, no login: useful for install-time aiming and for stores
  whose uplink is down. Colour-coded status badges; double-click a tile to focus.
- **Self-update** — checks GitHub Releases, downloads the new `--onedir` zip, **verifies its SHA-256**, and
  swaps itself in via a small `.bat` (which waits for the old process to release its DLLs). Refuses
  unsigned/undigested updates.
- **Stays out of the way** — system-tray icon, minimise-to-tray on close, and per-user login auto-start
  (`--minimized`), all without admin rights.

---

## Install (for store staff)

Download **`ChipmoSentryAgent-Setup.exe`** from the
[latest release](https://github.com/Chipmo-Sentry/sentry-agent-pc/releases/latest) and run it (per-user, no
admin prompt). On first launch the **pairing dialog** opens — paste the 6-digit code from the web app and
the store's cameras start streaming.

---

## Develop

```bash
uv sync
uv run sentry-agent-pc gui        # launch the desktop app

# CLI (power users / dev-token mode)
uv run sentry-agent-pc discover   # scan + register cameras
uv run sentry-agent-pc list
uv run sentry-agent-pc status
```

```
src/sentry_agent_pc/
├── main.py / gui_main.py     — CLI (Typer) + GUI entry points
├── backend_client.py         — httpx client (agent-scoped + legacy dev-token modes)
├── state.py                  — encrypted state v2 (agent JWT, store, camera list)
├── settings.py / config_file.py — %APPDATA%/Chipmo/sentry-agent config
├── autostart.py              — HKCU Run auto-start
├── updater.py                — GitHub-release self-updater (SHA-256 verified)
├── discovery/                — onvif, rtsp_probe, manual (brand templates), rtsp_paths
├── services/discovery_service.py — scan orchestration + state persistence
├── streaming/                — pusher (per-camera ffmpeg relay), controller (stream-config sync)
└── gui/                      — app, scan_dialog, add_dialog, local_view, live_view, update_dialog,
                               tray, widgets
```

### Build the `.exe`

```powershell
.\scripts\build_exe.ps1        # PyInstaller --onedir --windowed → dist/ChipmoSentryAgent/
```

`--onedir` (not `--onefile`) is deliberate: it ships DLLs alongside the exe so there's no per-launch temp
extraction and the self-updater can replace the folder reliably. `release.yml` builds the onedir zip + its
`.sha256` sidecar + the Inno Setup installer (`installer/ChipmoSentry.iss`) and publishes all three on a
`v*` tag.

### Test, lint, type-check

```bash
uv run pytest                  # onvif parser, rtsp probe/resolver, pusher, state, updater,
                               # autostart, brand templates, local view
uv run ruff check . && uv run mypy src/sentry_agent_pc
```

---

## Architecture notes

- **No AI on the agent.** The agent only discovers, pairs, and relays — every alert decision is made by
  [sentry-ai](https://github.com/Chipmo-Sentry/sentry-ai) on the GPU host. (Edge AI is a separate M3 story
  for the Pi agents.)
- **RTSP over TCP** everywhere (UDP is unreliable on busy store LANs).
- **Encrypted, reboot-stable state** means a paired agent reconnects on its own after a power cut.

---

## Related repos

- [sentry-backend](https://github.com/Chipmo-Sentry/sentry-backend) — pairing, camera registration, stream-config
- [sentry-ingest](https://github.com/Chipmo-Sentry/sentry-ingest) — the MediaMTX the agent pushes to
- [sentry-agent-pizero2w](https://github.com/Chipmo-Sentry/sentry-agent-pizero2w) · [sentry-agent-pi5](https://github.com/Chipmo-Sentry/sentry-agent-pi5) — the hardware-edge siblings (M3)

Platform overview: [Sentry-v.3 README](../README.md). Deep-dive: [docs/15-AGENT-PC.md](../docs/15-AGENT-PC.md).
