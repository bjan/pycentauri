# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.2] - 2026-04-22

### Changed
- Complete `PrintInfo.Status` code table, lifted verbatim from the
  authoritative enum in Elegoo's `elegoo-link` SDK. Previously the web
  UI (and the `pycentauri.models.PrintStatus` constants) only covered
  about half the codes — which meant users hit `CODE·20` literally
  during the routine PREHEAT-DONE transition between prints, and
  similarly for 11, 14, 15, 16, 17, 19, 21, 22, 23–26.
- Added a distinct visual class for the ERROR state (code 14) in the
  web UI so it renders in red with a soft glow instead of falling into
  the generic "unknown" bucket.
- Renamed a couple of previously-incorrect labels: code 12 was shown as
  "PREPARING", it's actually "RESUMING"; code 18 was shown as
  "RESUMED", it's actually "PRINT START" — both per the SDK.

## [0.4.1] - 2026-04-22

### Changed
- README audited end-to-end for accuracy: removed claims about a
  `~/.config/pycentauri/config.toml` loader that was never implemented,
  an `upload_file` / `list_files` MCP tool that doesn't exist, and a
  `centauri files` command that was a stub. Added the RTSP endpoints
  (`/api/rtsp/*`) and the FastAPI auto-docs URLs to the endpoint table,
  plus a note that the RTSP bridge itself isn't gated by
  `--enable-control`.
- Updated the package tagline on PyPI and in the module docstring to
  reflect all six surfaces rather than just three.

### Removed
- The stub `centauri files` CLI command. It always exited with
  "not yet supported" and was only there to reserve the name; not
  worth the README lie.

## [0.4.0] - 2026-04-22

### Added
- **RTSP bridge.** Re-stream the printer's MJPEG webcam as RTSP/H.264
  so VLC, Home Assistant, Jellyfin, Frigate, Synology Surveillance, and
  any other RTSP client can consume it. Powered by `MediaMTX` +
  on-demand `ffmpeg` transcode — the transcoder only runs while at least
  one client is connected, so idle cost is zero. Two ways to drive it:
  - **Standalone**: `centauri rtsp --host <printer>` runs in the
    foreground until Ctrl-C. Flags: `--port`, `--bind`, `--path`,
    `--fps`, `--bitrate`, `--preset`, `--webrtc/--no-webrtc`,
    `--hls/--no-hls`, and `--mediamtx-path` / `--ffmpeg-path`
    overrides.
  - **Integrated with `centauri server --rtsp`**: adds a "STREAM" panel
    to the built-in web UI with start/stop buttons, a copy-URL button,
    and live status. New endpoints: `GET /api/rtsp`,
    `POST /api/rtsp/start`, `POST /api/rtsp/stop`.
- Requires `mediamtx` and `ffmpeg` on `$PATH` — clear install hints
  surface in the API response and the UI panel if either is missing,
  and nothing changes for users who don't enable the feature.

## [0.3.1] - 2026-04-22

### Changed
- **Web UI redesign.** Moved from the generic dark-dashboard look to an
  industrial instrument-panel aesthetic — amber-on-near-black, viewfinder
  corner brackets and a faint crosshair over the webcam, ruler-style
  progress bar with diagonal-hatch fill, tabular-numeric temperature
  gauges with per-channel setpoint bars, kinematics readout with X/Y/Z
  cells and fan/Z-offset/mainboard telemetry, keyboard-key-style F1/F2/F3
  control buttons (with real keyboard shortcuts). Typography: Space Mono
  for display numerals, IBM Plex Mono for data, IBM Plex Sans Condensed
  for SCADA-style labels.
- Webcam MJPEG element now auto-recovers if the stream stalls for 15s.
- Added a favicon so browsers stop 404-ing `/favicon.ico`.

## [0.3.0] - 2026-04-22

### Added
- **Web UI** at `GET /ui/` (and `GET /` redirects there). Single static
  dashboard bundled in the wheel — no build step, no framework. Shows
  live webcam (MJPEG), progress bar, layer counter, temperature cards,
  position, fans, and Z-offset. Pause / Resume / Stop buttons auto-appear
  when the server is launched with `--enable-control`.
- `GET /stream` — MJPEG proxy through the API server to the printer's
  `:3031/video`. Browsers render it directly in an `<img>` tag.
- `GET /api/info` — JSON health endpoint, matching what `GET /` used to
  return before it was repurposed to redirect to `/ui/`.

### Changed
- `GET /` now redirects to `/ui/` (307). If the web assets aren't found
  in the wheel (custom build, etc.), `/` falls back to a minimal JSON
  health response so scripted clients don't break.

## [0.2.0] - 2026-04-22

### Added
- **HTTP + SSE server.** `centauri server [--host IP] [--bind 127.0.0.1]
  [--port 8787] [--enable-control]` runs a FastAPI app that wraps the
  same client library used by the CLI and MCP server:
  - `GET /` — health, version, connection state
  - `GET /status` — latest status snapshot (JSON)
  - `GET /attributes` — printer attributes (JSON)
  - `GET /snapshot` — single JPEG frame from the webcam
  - `GET /discover` — UDP LAN scan
  - `GET /events/status` — Server-Sent Events stream of live status pushes
  - `POST /print/{start,pause,resume,stop}` — only registered with
    `--enable-control` (mirrors the MCP security posture — the routes
    literally don't exist without the flag)
- New optional extra: `pip install 'pycentauri[server]'` pulls in
  FastAPI + uvicorn + sse-starlette.
- The server holds a single long-lived WebSocket for its lifetime with
  auto-reconnect and exponential backoff, so it never bumps against the
  printer's 5-slot limit and HTTP requests return cached pushes instantly.

## [0.1.1] - 2026-04-22

### Fixed
- `Printer.status()`, `attributes()`, and all control methods hung
  indefinitely when the printer was in a paused or errored state. The
  firmware doesn't push `Attributes` spontaneously outside idle/active
  states, and every SDCP command needs a `MainboardID` in its envelope,
  so the client would deadlock waiting for a push that never comes.
- The CLI and MCP server now pre-discover the printer over UDP before
  opening the WebSocket and pass the mainboard ID into `Printer.connect()`.

### Added
- `Printer.connect(..., mainboard_id=...)` — pre-seed the mainboard ID
  (e.g. from a prior `discover()`) so the client can send commands
  immediately, without waiting for the printer's first `Attributes` push.
- `Printer.wait_for_mainboard()` now raises a `PrinterError` with a
  pointer at the `mainboard_id=` workaround instead of a bare
  `asyncio.TimeoutError`.

### Changed
- `discover()` retransmits the probe multiple times within the timeout
  window, improving reliability on busy or lossy networks. Also binds
  explicitly to `0.0.0.0` so loopback delivery works on macOS.

### Known limits
- Elegoo firmware accepts at most **5 concurrent WebSocket connections**
  on port 3030. The 6th attempt is rejected at the HTTP upgrade with
  `HTTP 500 "too many client"`. Slots release immediately on close.

## [0.1.0] - 2026-04-22

### Added
- Async Python client for Elegoo Centauri Carbon printers speaking SDCP v3 over
  WebSocket (`ws://<host>:3030/websocket`).
- UDP broadcast discovery on port 3000 with the `M99999` probe.
- MJPEG snapshot grabber for the built-in webcam
  (`/network-device-manager/network/camera`).
- `centauri` CLI with `discover`, `status`, `watch`, `snapshot`, `attributes`,
  `files`, `print {start,pause,resume,stop}`, `upload`, `mcp`.
- Optional MCP server (`python -m pycentauri.mcp`) with read-only tools by
  default; control tools registered only when `--enable-control` is set.
- Apache-2.0 license.
