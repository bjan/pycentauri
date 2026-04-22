# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
