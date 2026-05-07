# Architecture

`pycentauri` is six surfaces over one async client. This doc maps the
modules, the request flows, and the lifetime of every connection.

```
                                            ┌──────────────────────────┐
   ┌── library users (import) ──┐           │     printer at LAN IP    │
   │                            │           │  ┌────────────────────┐  │
   │  centauri  (CLI)  ─────────┼───────┐   │  │ port 80   web UI   │  │
   │                            │       │   │  │ port 3030 SDCP WS  │  │
   │  python -m pycentauri.mcp  ├───────┤   │  │ port 3031 MJPEG    │  │
   │  (MCP stdio)               │       │   │  │ port 3000 UDP disc │  │
   │                            │   ┌───▼───▼──┴────────────┐       │  │
   │  centauri server  ─────────┤   │  pycentauri.client    │  ─────┘  │
   │  ├─ /api/* (REST/SSE)      │   │  (async WS, RtspGate) │
   │  ├─ /ui/   (static)        │   └───────────────────────┘
   │  └─ /api/rtsp/* ───────────┤              │
   │                            │              │  ffmpeg + MediaMTX
   │  centauri rtsp  ──────────┐│              │  (subprocess; OS bins)
   └────────────────────────────┘              │
                                               ▼
                                    rtsp://host:8554/printer
```

## Modules

| Module | Owns | Touches the printer? |
|---|---|---|
| `sdcp` | Wire envelope: `build_request`, `parse_message`, `Cmd` enum | No (pure transform) |
| `discovery` | UDP `M99999` broadcast + JSON parse | Yes (UDP only) |
| `camera` | MJPEG frame grabber (HTTP single-shot) | Yes (HTTP) |
| `client` | `Printer` async WS client; reader task; request/response correlation; control gate | Yes (WS) |
| `models` | `Status`, `Attributes`, `PrintInfo`, `PrintStatus` codes | No |
| `cli` | Typer subcommands; auto-discovery + mainboard pre-seed | Indirect |
| `server` | FastAPI app, `PrinterManager` (long-lived WS), `RtspController`, `/stream` proxy, web UI mount | Yes (one WS) |
| `rtsp` | MediaMTX config render + subprocess management | No (manages subprocess that does) |
| `mcp.server` | FastMCP tools | Indirect (one WS per call) |
| `web/` | Static HTML/CSS/JS dashboard | Through the server's REST/SSE |

`models` and `sdcp` are the only modules that have no I/O — everything
else is built on top of them.

## Connection lifetime by entrypoint

| Entrypoint | WS strategy |
|---|---|
| `centauri <subcommand>` | One WS per invocation: open → discover → command → close |
| `python -m pycentauri.mcp` (a tool call) | One WS per tool invocation. Cached `PYCENTAURI_MAINBOARD_ID` env between calls in the same process |
| `centauri server` | **One** long-lived WS held by `PrinterManager` for the app's lifetime. Auto-reconnects with exponential backoff (1 s → 30 s) |
| `centauri rtsp` | Zero direct WS to the printer. Reads MJPEG over HTTP via the MediaMTX→ffmpeg pipeline |

The 5-slot firmware limit is the reason the server holds one persistent
connection rather than reconnecting per request.

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

`status()` returns immediately if a push has already arrived (the reader
populates `_latest_status` continuously). Otherwise it triggers a fresh
subscribe (`Cmd 512`) and waits for the next push.

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
Cmd 129/131/etc → ack). All other test files build on it via
monkey-patched `WS_PORT`.

There are **no** live-printer requirements in CI. The optional live
suite under `tests/integration/` runs only when `PYCENTAURI_TEST_HOST`
is set and is intentionally excluded from the default `pytest`
collection in CI.

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
