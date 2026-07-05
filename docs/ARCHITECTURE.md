# Architecture

`pycentauri` is six surfaces over one async client. This doc maps the
modules, the request flows, and the lifetime of every connection.

```
                                        ┌─────────────────────────────────┐
   ┌── library users (import) ──┐       │        printer at LAN IP        │
   │                            │       │  CC1:              CC2:         │
   │  centauri  (CLI)  ─────────┼───┐   │  ┌──────────────┐ ┌───────────┐ │
   │                            │   │   │  │ 80   web UI  │ │ 80  HTTP  │ │
   │  python -m pycentauri.mcp  ├───┤   │  │ 3030 SDCP WS │ │ 1883 MQTT │ │
   │  (MCP stdio)               │   │   │  │ 3031 MJPEG   │ │ 8080 MJPEG│ │
   │                            │   │   │  │ 3000 UDP disc│ └───────────┘ │
   │  centauri server  ─────────┤   │   │  └──────────────┘               │
   │  ├─ /api/* (REST/SSE)      │   ▼   └──────▲──────────────▲───────────┘
   │  ├─ /ui/   (static)        │ connect_auto─┴──────────────┘
   │  └─ /api/rtsp/* ───────────┤   ├─ :1883 open → cc2.CC2Printer (MQTT)  CC2
   │                            │   └─ else       → client.Printer (WS)    CC1
   │  centauri rtsp  ──────────┐│              │
   └────────────────────────────┘              │  ffmpeg + MediaMTX
                                               │  (subprocess; OS bins)
                                               ▼
                                    rtsp://host:8554/printer
```

## Modules

| Module | Owns | Touches the printer? |
|---|---|---|
| `sdcp` | CC1 wire envelope: `build_request`, `parse_message`, `Cmd` enum | No (pure transform) |
| `discovery` | UDP `M99999` broadcast + JSON parse (finds CC1s only) | Yes (UDP only) |
| `camera` | MJPEG frame grabber; `CAMERA_PORT` (CC1 :3031) / `CAMERA_PORT_CC2` (:8080) | Yes (HTTP) |
| `client` | `Printer` — CC1 async WS client; reader task; request/response correlation; control gate; base API both models share | Yes (WS) |
| `cc2` | `CC2Printer(Printer)` — MQTT transport, JSON-RPC envelope, CC2→CC1 payload translation, Canvas | Yes (MQTT + HTTP bootstrap) |
| `connect` | `connect_auto()` — probes :1883 (CC2), else connects CC1 WS directly (no throwaway :3030 probe) | Yes (TCP probe) |
| `models` | `Status`, `Attributes`, `PrintInfo`, `CanvasStatus`, `PrintStatus` codes | No |
| `cli` | Typer subcommands; auto-discovery + mainboard pre-seed; `--access-code` plumbing | Indirect |
| `server` | FastAPI app, `PrinterManager` (long-lived connection), `RtspController`, `/stream` proxy, web UI mount | Yes (one connection) |
| `rtsp` | MediaMTX config render + subprocess management | No (manages subprocess that does) |
| `mcp.server` | FastMCP tools | Indirect (one connection per call) |
| `web/` | Static HTML/CSS/JS dashboard | Through the server's REST/SSE |

`models` and `sdcp` are the only modules that have no I/O — everything
else is built on top of them.

## Connection lifetime by entrypoint

| Entrypoint | Connection strategy |
|---|---|
| `centauri <subcommand>` | One connection per invocation: probe → open → command → close (WS for CC1, MQTT session for CC2) |
| `python -m pycentauri.mcp` (a tool call) | One connection per tool invocation. Cached `PYCENTAURI_MAINBOARD_ID` env between calls in the same process |
| `centauri server` | **One** long-lived connection held by `PrinterManager` for the app's lifetime. Auto-reconnects with exponential backoff (1 s → 30 s); on CC2, paho's own reconnect also re-registers with the printer from `_on_connect` |
| `centauri rtsp` | Zero control connection to the printer. Reads MJPEG over HTTP via the MediaMTX→ffmpeg pipeline (camera port picked by probing :1883 only — :3030 is never touched) |

The CC1's 5-slot firmware limit is the reason the server holds one
persistent connection rather than reconnecting per request.

## Request flow: `GET /status` (server)

```
HTTP client
    │
    ▼
FastAPI route ── PrinterManager.printer ──► Printer.status()
                                                  │
                              (subscribed already, _latest_status set)
                                                  │
                                                  ▼
                                         Status.from_payload(raw)
                                                  │
                                                  ▼
                                              JSON body
```

On **CC1**, `status()` returns immediately if a push has already arrived
(the reader populates `_latest_status` continuously); otherwise it
triggers a fresh subscribe (`Cmd 512`) and waits for the next push.

On **CC2**, `status()` round-trips a method-1002 request every call —
there is no subscribe-and-cache shortcut. The result also refreshes the
baseline that method-6000 partial deltas are merged into. Callers that
poll `/status` frequently (the web UI polls at 2 s while printing)
should know each poll is a real MQTT request; the CC2's rate limiter
tolerates this cadence but not much more.

## Request flow: SSE `/events/status`

The reader broadcasts every `STATUS` message to every queue in
`Printer._status_queues`. `Printer.watch()` registers a queue, yields
`Status` objects until the consumer breaks, then deregisters. The
SSE endpoint wraps `watch()` in `EventSourceResponse`.

## Request flow: `/api/rtsp/start`

```
POST /api/rtsp/start
    │
    ▼
RtspController.start()
    │
    ├── ensure_binaries()  # mediamtx + ffmpeg
    ├── _mediamtx_yaml()   # render runOnDemand command
    ├── start_detached()   # subprocess.Popen → mediamtx
    │
    ▼
Returns /api/rtsp state JSON

(Later, a client connects to rtsp://host:8554/printer
 → MediaMTX runs the ffmpeg subprocess that pulls printer:3031/video,
   transcodes to H.264, hands the H.264 bitstream back to MediaMTX,
   which serves it as RTSP.)
```

`runOnDemand` keeps the ffmpeg child idle until the first reader
connects, so an RTSP server with no readers costs nothing.

## Lifespans

The FastAPI `lifespan` context manager:

1. Constructs `PrinterManager` and `RtspController` (when `--rtsp`).
2. Calls `manager.start()` — kicks the supervisor task and waits up to
   10 s for the first connection (don't block forever; endpoints will
   503 cleanly until ready).
3. Yields control to uvicorn.
4. On shutdown: `RtspController.stop()` first (terminate child + clean
   up tempfile), then `PrinterManager.stop()` (cancel supervisor +
   close WS).

Background tasks all use `asyncio.create_task` with a name and are
cancelled in `stop()` with `contextlib.suppress(CancelledError)`.

## Why the web UI lives in the package

The web assets are shipped inside the wheel at `pycentauri/web/` and
served via `StaticFiles(directory=resource_files("pycentauri") /
"web", html=True)`. This means:

- Single `pip install` puts the UI on disk; no separate frontend build.
- The user can replace the directory at runtime if they want to fork the
  UI without modifying the package.
- The fallback `/` route serves a JSON health blob if the `web/`
  directory is somehow missing (e.g. an unusual install layout) — so
  scripted clients don't break.

## Test architecture

`tests/test_client.py` defines `_FakePrinter` — an in-process
`websockets.asyncio.server` that speaks just enough SDCP to exercise the
real client (Attributes push on connect, Cmd 512 → status pushes,
Cmd 129/131/etc → ack). The server tests build on it via monkey-patched
`WS_PORT` and a fixture that bypasses `connect_auto`'s port probing.
`tests/test_cc2_mapping.py` covers the CC2→CC1 translation layer
(state mapping, delta merge, Canvas parsing) as pure dict-in/dict-out
functions — no MQTT infrastructure.

There are **no** live-printer requirements in CI, and no automated live
suite exists yet — the CC2 MQTT transport itself is currently verified
manually with the CLI against real hardware (`centauri status`,
`centauri canvas`, a fan write) after changes.

## Surface dependency matrix

| Surface | Requires (Python) | Requires (system) |
|---|---|---|
| Library | base deps only | — |
| CLI | base deps only | — |
| MCP server | `pycentauri[mcp]` | — |
| HTTP server + UI | `pycentauri[server]` | — |
| RTSP bridge | (no Python extra) | `mediamtx`, `ffmpeg` |

The RTSP bridge intentionally has no Python extra — the only deps are
the system binaries. The CLI subcommand is therefore always present;
it just refuses to run with a clean install hint if the binaries are
missing.
