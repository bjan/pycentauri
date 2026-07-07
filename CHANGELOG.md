# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.6.5] - 2026-07-06

### Fixed
- CC2 connect now surfaces a clear error when the printer's HTTP API is
  unreachable instead of leaking a raw `httpx.ConnectError`. The CC2
  gates its local API behind **"LAN Only" mode** — with it off, the
  printer works through Elegoo's cloud and leaves port 80 closed, so the
  serial-number bootstrap fails even though MQTT :1883 answers. The
  error now says exactly that and names the setting to change. (Reported
  by a user on firmware 02.00.02.00 whose connection worked the moment
  they enabled LAN Only.)

### Documentation
- README and `docs/PROTOCOL.md` now document that "LAN Only" mode must
  be enabled on the CC2 for local control, and note that firmware
  02.00.02.00 is a lockdown release (removes SSH, blocks downgrades)
  with a community repacked v01.03.02.51 available if needed.


## [0.6.4] - 2026-07-05

### Fixed
- `GET /openapi.json` (and therefore the `/docs` Swagger UI) returned a
  500 instead of the API schema. The Canvas `RefillBody` request model
  was defined inside `create_app()`, so under
  `from __future__ import annotations` its forward references couldn't be
  resolved at schema-generation time (request validation still worked,
  which is why nothing else surfaced it). Moved it to module scope with
  the other request-body models; added a test that renders the schema.


## [0.6.3] - 2026-07-05

### Changed
- **CC2 speed pinning now tells a human's balanced apart from a firmware
  reset — no more Auto escape hatch.** The firmware resets speed_mode to
  balanced as the leading edge of every Canvas filament switch, a few
  seconds before the head parks; measured reset→park lead times are
  tight (6–8 s, never above ~9 s). pycentauri uses that: a drop to
  balanced followed by a park within ~12 s is the firmware and the
  pinned mode is re-applied when the switch completes; a drop that sits
  at balanced for ~12 s with no park is a human tapping balanced on the
  touchscreen, so the pin is released and balanced is honored. A
  sustained non-balanced touchscreen mode is still adopted as the pin
  outright. Both paths live-verified 2026-07-05 (firmware re-apply and
  human release, each with journal-logged decisions).
- **Removed the `auto` speed mode / Auto button.** It existed only to
  release the pin for the touchscreen-balanced case, which is now
  detected automatically. `speed auto`, `{"mode": "auto"}`, and the web
  UI Auto button are gone.


## [0.6.2] - 2026-07-05

### Changed
- **CC2 speed-mode persistence redesigned as pin-and-enforce.** The
  0.6.1 snapshot-per-switch restore lost an arms race with the
  firmware: after eight consecutive successful restores, a reset whose
  lead time exceeded the debounce window poisoned the baseline and
  silently disabled every restore after it. The firmware resets
  `speed_mode` to balanced as part of every Canvas filament switch —
  the reset fires several seconds before the head parks, while the
  status still reads "printing", and its lead time varies — and it only
  ever resets TO balanced. So the model is now timing-agnostic: the
  mode you set is pinned (immediately via pycentauri; after holding a
  few seconds when set from the touchscreen, since a non-balanced mode
  can only come from a human), and any disagreement lasting 12 s while
  printing is re-applied, at most every 30 s. One consequence,
  deliberate: while a mode is pinned, selecting *balanced* from the
  touchscreen is byte-identical on the wire to a firmware reset and will
  be reverted — switch to balanced through pycentauri instead (which
  re-pins), or release the pin. Pins clear when the print ends, and
  ``speed auto`` (CLI), ``{"mode": "auto"}`` (HTTP), or the web UI's
  Auto button release the pin explicitly, returning speed control to
  the printer.

## [0.6.1] - 2026-07-05

### Fixed
- **CC2: the user's speed mode now survives Canvas filament switches.**
  The firmware resets `speed_mode` to balanced on every mid-print
  switch and leaves it there (verified 2026-07-05: mode stayed reset
  after the switch completed). `CC2Printer` now snapshots the mode in
  effect when a switch begins and re-applies it once printing resumes —
  regardless of whether the mode was set via pycentauri or the
  touchscreen. Requires `enable_control`; read-only sessions log the
  reset instead. The restore baseline is debounced (a mode must hold
  15 s to count) because the firmware fires its reset several seconds
  *before* the head parks — without the debounce, that transient
  poisoned the snapshot. Verified across two hands-free switch cycles
  with journal-logged restores ~3 s after each resume.
- `centauri server` now surfaces pycentauri's own log lines (speed-mode
  restores, reconnects) in its output — previously only uvicorn's
  loggers were configured and library INFO logs were invisible.

## [0.6.0] - 2026-07-05

### Added
- **Centauri Carbon 2 support.** The CC2 uses MQTT on port 1883
  (instead of the CC1's WebSocket SDCP on 3030) with a JSON-RPC
  envelope, X-Token auth, and a completely different command set
  (1001/1002/1020–1031 vs 0/1/128–512). This release adds:
  - `CC2Printer` — async MQTT client that subclasses `Printer`,
    translating CC2 payloads into the same `Status` / `Attributes`
    models so every surface (CLI, HTTP, MCP, web UI) works unchanged.
  - `connect_auto(host, access_code=...)` — factory that detects the
    model (probes `:1883` only — a CC1 answers with a harmless kernel
    RST, and the CC1's real WebSocket connect doubles as its own probe)
    and returns the right `Printer` subclass. Callers never need to
    know which model they're talking to.
  - `--access-code` / `PYCENTAURI_ACCESS_CODE` on every CLI command
    and MCP launch (required for CC2, ignored for CC1).
  - `paho-mqtt>=2.0` added to core dependencies.
- CC2 status exposes `gcode_move.speed` — live head speed as the
  commanded speed of the current move, in mm/min (÷60 matches the
  printer screen's mm/s readout)
  and `speed_mode` (integer 0–3) — fields the CC1 lacks entirely. Both
  are preserved in `Status.raw["_cc2"]` alongside `remaining_time_sec`,
  `filament_detected`, `external_device`, and `exception_status`.
- CC2 methods 1028 (SET_TEMPERATURE), 1029 (SET_LIGHT), 1030
  (SET_FAN_SPEED), 1031 (SET_PRINT_SPEED), 1023 (RESUME), and 1036
  (PRINT_TASK_LIST) all confirmed working. 1042 (VIDEO_STREAM) and
  1044 (GET_FILE_LIST) are non-responsive on firmware 01.03.02.51.
- **Canvas multi-filament system integration (CC2 only):**
  - `Printer.canvas_status()` — returns `CanvasStatus` with connected
    units, tray list (filament name/type/color/brand/temp range/loaded
    status), active tray, and auto-refill state. Method 2005.
  - `Printer.set_auto_refill(enabled)` — toggles auto-refill. Method
    2004. Gated by `enable_control`.
  - CLI: `centauri canvas` (read) + `centauri refill --on/--off`
    (write, requires `--enable-control`).
  - HTTP: `GET /canvas` (always) + `POST /canvas/refill` (control-gated).
  - MCP: `get_canvas_status` (always) + `set_auto_refill` (control-gated).
  - `CanvasStatus`, `CanvasUnit`, `CanvasTray` model classes exported
    from the package for typed access.

### Fixed (pre-release hardening — none of these shipped in an earlier release)
- **CC1 `status()`/`watch()` self-heal through a wedged push
  scheduler.** Discovered live 2026-07-04 on V0.3.0-o: the firmware can
  enter a state where `Cmd 512` subscribes are accepted but no status
  frames are ever pushed, while the request path stays healthy — and a
  reboot does not clear it (verified 2026-07-05). Both methods now fall
  back to explicit `Cmd 0` status requests (documented to emit a
  one-shot status frame) whenever a subscribe produces no push within
  one period, instead of hanging forever. See `docs/PROTOCOL.md`
  "Operational quirks".
- `CC2Printer.watch()` now polls method 1002 inline whenever pushes go
  quiet instead of silently terminating after one push period — CC2
  `centauri watch` and the SSE stream no longer die on an idle printer.
- MQTT responses are correlated by the client's exact `api_response`
  topic and non-push method. Previously any client's response — or the
  printer's own 6000/6008 broadcasts, which reuse small auto-increment
  ids — could resolve a pending request future with the wrong payload
  (including reporting success for a control command).
- Registration is re-published from `_on_connect`, so paho's automatic
  MQTT reconnect re-registers with the printer and pushes survive a
  printer reboot without restarting `centauri server`.
- All MQTT-callback work that touches futures, events, queues, or the
  merged status dict is marshalled onto the asyncio loop (fixes the
  cross-thread delta merge and late `set_result` races).
- `GET /stream` uses a bounded connect timeout (read stays unbounded
  for MJPEG) instead of hanging forever against a wedged camera port.
- `GET /canvas` returns 504 for printer timeouts, reserving 501 for
  "no Canvas on this printer"; the web UI now retries transient Canvas
  failures instead of hiding the panel until a page reload.
- A wrong CC2 access code now raises a clear auth error (MQTT reason
  code / HTTP 401 hint) instead of a generic timeout or raw traceback.
- `connect_auto()` and `centauri rtsp` no longer probe :3030 — the CC1
  WebSocket connect doubles as the probe, avoiding the connect/close
  churn that port is sensitive to. `centauri rtsp` and the HTTP server
  both pick the right MJPEG port (CC1 :3031 / CC2 :8080) automatically,
  including reconnects after starting with the printer offline.
- Unknown CC2 `machine_status` values map to sentinel code 99 instead
  of leaking raw values into the CC1 status-code space; `PrintStatus`
  gains constants for the CC2 codes 27/28/29.
- `CanvasStatus.from_payload` survives malformed/absent `canvas_info`
  payloads; `CanvasUnit`/`CanvasTray` are exported from the package
  root; `typing_extensions` declared as a direct dependency.
- **CC2 sessions send the app-level `{"type": "PING"}` keepalive every
  30 s.** The firmware expires a client's registration after several
  quiet minutes and then silently stops answering that session's
  requests — the MQTT connection stays up, responses just never come
  (verified 2026-07-05: a dashboard went request-deaf after ~6 minutes
  while a fresh session answered instantly). MQTT-level keepalive does
  not prevent this; only the app-level PING does.
- `centauri server` bounds uvicorn's graceful shutdown at 5 s. Open
  SSE/MJPEG streams never close on their own, and without the bound a
  stopped server lingered forever as a zombie still holding a printer
  connection (observed 2026-06-23 and 2026-07-05).
- CC2 lifecycle commands (start/pause/stop/resume) use a 90 s timeout —
  the firmware answers only after the mechanical sequence completes
  (observed 2026-07-05: a resume succeeded but its confirmation
  arrived after the old 15 s window, surfacing a false error). When a
  confirmation still doesn't arrive, `POST /print/*` returns 504
  ("sent, unconfirmed — check printer status") instead of 502, and the
  web UI shows it as a warning rather than a failure.

### Tests
- New `tests/test_cc2_mapping.py`: pure dict-in/dict-out coverage of
  the CC2→CC1 translation layer — machine_status/sub_status mapping
  (including purge-zone filament-switch detection and its progress
  guard), partial-delta deep merge, full 1002 payload round-trip, and
  Canvas parsing. Plus a server test pinning `GET /canvas` → 501 on
  CC1.

### Documentation
- `docs/PROTOCOL.md` gains a full CC2 section: MQTT transport, topic
  structure, connection/registration dance, the confirmed method table
  (12 probed, 10 working), error codes, the complete status payload,
  and a CC1-vs-CC2 comparison table. Probed on 2026-06-30 through
  2026-07-04 against firmware 01.03.02.51 (Canvas methods 2004/2005
  verified live with a 4-slot Canvas attached; CC2 webcam confirmed
  unauthenticated MJPEG on :8080).
- `docs/ARCHITECTURE.md` rewritten for the two-transport reality:
  module map with `cc2`/`connect`, per-model ports, CC1-vs-CC2
  `/status` and SSE flow differences, and an honest test-architecture
  section (no automated live suite exists; live verification is manual
  CLI runs).

## [0.5.1] - 2026-06-17

### Added
- **CLI parity with the new live-adjust API.** Three new top-level
  commands matching the library and HTTP surfaces shipped in 0.5.0:
  - `centauri speed <silent|balanced|sport|ludicrous|50|100|130|160>`
  - `centauri fan [--model N] [--aux N] [--chamber N]`
  - `centauri temp [--nozzle N] [--bed N] [--chamber N]`
  All require `--enable-control` and accept any subset of fan/heater
  flags (omitted axes are left untouched).
- **MCP tool parity:** three new tools (only registered with
  `--enable-control`) — `set_print_speed`, `set_fan_speed`,
  `set_temperatures`. Same shapes as the HTTP endpoints; not marked
  "DESTRUCTIVE" because live runtime adjustment is their entire
  purpose.

### Fixed
- 0.5.0 shipped the live-adjust API to the library, HTTP server, and
  web UI but missed the CLI and MCP server. This release closes that
  parity gap — every surface now exposes the same control set.

## [0.5.0] - 2026-06-17

### Added
- **Live print-parameter adjust (`Cmd 403` family).** All three payload
  variants confirmed working against firmware V0.3.0-o:
  - `Printer.set_print_speed(mode)` — sets the printer's speed mode.
    Accepts a name (`"silent"`, `"balanced"`, `"sport"`, `"ludicrous"`)
    or its canonical `PrintSpeedPct` value (`50`, `100`, `130`, `160`).
    Only those four values are accepted by the firmware — arbitrary
    intermediate percentages return `Ack=0` but are silently dropped,
    and the mode change only takes effect while a print is actively
    running. Names + values lifted directly from the printer's own
    SPA i18n file at `/app/resources/www/assets/i18n/network-en.json`.
  - `Printer.set_fan_speed(model=, auxiliary=, chamber=)` — set any
    subset of the model / auxiliary / chamber fan (0..100% each).
  - `Printer.set_temperatures(nozzle=, bed=, chamber=)` — heater
    targets with safety caps (nozzle 0..300, bed 0..110, chamber 0..60).
    `0` turns the heater off.
- HTTP endpoints (only when launched with `--enable-control`):
  `POST /print/speed`, `POST /print/fan`, `POST /print/temperature`.
  Speed body: `{"mode": "silent|balanced|sport|ludicrous"}` (or the
  integer equivalent).
- Web UI `ADJUST` panel with three sections: a 4-button speed-mode
  selector (the active mode pulses amber based on live status), per-fan
  rows (model / aux / chamber, 0–100%), and per-heater rows with
  per-row APPLY buttons and confirm prompts on high temps
  (nozzle > 240 °C, bed > 85 °C).
- Web UI auto-hydrates the fan/heater sliders + inputs from each status
  push, so they always start at the printer's actual live values
  instead of zero. Controls that are currently focused are skipped so
  the live update doesn't yank a value out from under a drag/type.
- Adaptive backup poll: 2-second cadence while the printer is actively
  printing, 10-second cadence when idle/paused/done/errored. Switches
  immediately on state transition. SSE remains the primary update
  path; the poll is a safety net for Firefox's silent SSE stalls.
- `sdcp.Cmd.CHANGE_PRINT_PARAMS = 403` enum entry, plus the previously-
  unenumerated `GET_FILE_LIST = 258` and `GET_PRINT_HISTORY = 320` for
  reference (no client methods yet — exposing those is queued for a
  later release).
- `Printer.PRINT_SPEED_MODES` class-level map exposing the canonical
  `{mode_name: PrintSpeedPct}` table for callers that want to render
  their own mode picker.

### Fixed
- **Footer rail no longer overlaps the ADJUST panel on tall pages.**
  Removing the `min-height: 0` on `.console` lets the grid grow with
  its content (instead of being constrained to the body's flex slot),
  so the bottom rail reflows below the panel instead of hovering over
  it.
- **Web UI status now stays fresh in Firefox.** SSE remains the
  primary push path, but the new adaptive backup poll runs in parallel
  — Firefox occasionally drops the SSE stream silently and previously
  required a manual refresh to update; the poll keeps the progress
  bar, temps, fan readings, and ADJUST hydration live regardless.

### Documentation
- `docs/PROTOCOL.md` promotes Cmd 403 (all three payload variants) to
  the confirmed-working table, documents the four canonical
  `PrintSpeedPct` values, captures the printer's internal architecture
  as observed via SSH on OpenCentauri V0.3.0-o (the `app` binary
  embeds a Klipper-derived motion stack rather than running it as a
  separate process, which explains why an `app` crash kills any
  active print), adds the log-line signal table for filament cycles
  (`feed state change`, `M729`, etc.), and the recorded touchscreen
  tap-event sequence for the Goodix `gt9xxnew_ts` driver.

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
