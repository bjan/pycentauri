# pycentauri

Local-network toolkit for the [Elegoo Centauri
Carbon](https://www.elegoo.com/) 3D printer. Python library, CLI, MCP
server, REST/SSE server, built-in web UI, and an RTSP bridge — all
powered by the same async client.

`pycentauri` speaks the printer's native SDCP v3 protocol over its local
WebSocket (port 3030) — no cloud account required. It exposes six surfaces:

1. An **async Python library** for direct integration.
2. A **`centauri` CLI** for quick status checks, snapshots, and control.
3. An **MCP server** so AI agents (Claude Code, Claude Desktop, Cursor, any
   MCP-compatible client) can monitor and drive the printer as a tool.
4. An **HTTP + SSE server** for dashboards, reverse-proxy integration, and
   anything that wants a plain REST API.
5. A **built-in web UI** (industrial instrument-panel theme, mobile-friendly)
   served at `/ui/` by the HTTP server.
6. An **RTSP bridge** that re-streams the printer's MJPEG webcam as
   H.264/RTSP for Home Assistant, Jellyfin, VLC, Frigate, and NVRs.

> **Status:** alpha. The protocol has been reverse-engineered from the official
> [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) C++ SDK and the
> [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link) project. It
> works against the original Centauri Carbon on current firmware (tested on
> V1.1.46). The newer Centauri Carbon 2 (which uses MQTT) is not supported.

## Install

```sh
pip install pycentauri                    # library + CLI
pip install "pycentauri[mcp]"             # + MCP server
pip install "pycentauri[server]"          # + HTTP REST/SSE server + web UI
pip install "pycentauri[mcp,server]"      # all Python surfaces
```

The RTSP bridge additionally requires [MediaMTX](https://github.com/bluenviron/mediamtx/releases)
and `ffmpeg` on `$PATH` — see the RTSP section below for install hints.

## Quick start — CLI

```sh
# Find printers on your LAN
centauri discover

# One-shot status (pretty or JSON)
centauri status --host 192.168.1.209
centauri status --host 192.168.1.209 --json

# Stream live status updates
centauri watch --host 192.168.1.209

# Grab a webcam snapshot
centauri snapshot --host 192.168.1.209 shot.jpg

# Printer attributes (model, firmware, mainboard ID, capabilities)
centauri attributes --host 192.168.1.209

# Control actions — require --enable-control
centauri print start cube.gcode --host 192.168.1.209 --enable-control
centauri print pause            --host 192.168.1.209 --enable-control
centauri print resume           --host 192.168.1.209 --enable-control
centauri print stop             --host 192.168.1.209 --enable-control
```

The host can also come from the `PYCENTAURI_HOST` environment variable.
If neither is set, every command auto-discovers via a 2.5 s UDP broadcast
and bails out if it finds zero or more than one printer.

## Quick start — Python

```python
import asyncio
from pycentauri import Printer

async def main():
    async with await Printer.connect("192.168.1.209") as printer:
        status = await printer.status()
        print(status.print_status, status.progress, status.temp_nozzle)

        jpeg = await printer.snapshot()
        with open("shot.jpg", "wb") as f:
            f.write(jpeg)

        async for update in printer.watch():
            print(update.print_status, update.progress)

asyncio.run(main())
```

Control actions (`start_print`, `pause`, `resume`, `stop`) require
`Printer.connect(..., enable_control=True)`.

## Quick start — HTTP server

```sh
# Read-only, bound to loopback so only this box can hit it:
centauri server --host 192.168.1.209 --port 8787

# Read + write + RTSP, bound to all interfaces (put a reverse proxy in front):
centauri server --host 192.168.1.209 --bind 0.0.0.0 --port 8787 \
                --enable-control --rtsp
```

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Redirects to `/ui/` |
| `GET` | `/ui/` | Built-in web dashboard |
| `GET` | `/api/info` | Health + version (JSON) |
| `GET` | `/status` | Latest status push (cached; updates every ~5 s) |
| `GET` | `/attributes` | Printer attributes |
| `GET` | `/snapshot` | `image/jpeg` response |
| `GET` | `/stream` | MJPEG stream proxied from the printer (embeds in `<img>`) |
| `GET` | `/discover` | LAN scan |
| `GET` | `/events/status` | Server-Sent Events stream of pushes |
| `GET` | `/api/rtsp` | RTSP bridge state (when `--rtsp` is set) |
| `POST` | `/api/rtsp/{start,stop}` | Start/stop the RTSP bridge (when `--rtsp` is set) |
| `GET` | `/docs` / `/redoc` | Auto-generated OpenAPI docs |
| `POST` | `/print/start` | Body: `{"filename": "cube.gcode"}`. Requires `--enable-control`. |
| `POST` | `/print/{pause,resume,stop}` | Requires `--enable-control`. |

The server holds a single long-lived WebSocket to the printer and reuses
it for every request — no per-request reconnect, and it won't bump into
the firmware's 5-slot limit.

## Quick start — RTSP bridge

```sh
# Install system dependencies first:
#   mediamtx  https://github.com/bluenviron/mediamtx/releases  (or `brew install mediamtx`)
#   ffmpeg    `brew install ffmpeg` or `apt install ffmpeg`

# Standalone: foreground, Ctrl-C to stop
centauri rtsp --host 192.168.1.209
# → rtsp://<this-host>:8554/printer

# Or, integrated with the HTTP server + web UI
centauri server --host 192.168.1.209 --rtsp --bind 0.0.0.0
# The web UI gains a STREAM panel with a start/stop toggle, a copy-URL button,
# and live status: http://<this-host>:8787/ui/
```

Open that URL in **VLC** (Media → Open Network Stream), point **Home Assistant**
at it via the Generic Camera integration, or feed it to **Frigate** / **Jellyfin**
/ **Synology Surveillance** for NVR recording and motion detection.

MediaMTX runs the ffmpeg transcode only while at least one client is
actually connected, so idle cost is zero. Tunable flags: `--fps`,
`--bitrate`, `--preset`, `--path`, `--port`, `--bind` (standalone),
or `--rtsp-fps`, `--rtsp-bitrate`, `--rtsp-path`, `--rtsp-port`,
`--rtsp-bind` (on `centauri server`). Run `centauri rtsp --help` or
`centauri server --help` for the full list.

## Quick start — MCP

Register the server with your agent. With Claude Code:

```sh
claude mcp add pycentauri --env PYCENTAURI_HOST=192.168.1.209 \
    -- python -m pycentauri.mcp

# Or, with control actions enabled (gives the agent start/pause/resume/stop):
claude mcp add pycentauri --env PYCENTAURI_HOST=192.168.1.209 \
    -- python -m pycentauri.mcp --enable-control
```

Setting `PYCENTAURI_HOST` in the MCP server's launch env means the agent
can't be tricked into targeting an arbitrary IP through prompt injection —
the host is pinned at spawn time. Tools exposed:

| Tool | Always available | Description |
|---|---|---|
| `get_status` | yes | State, temperatures, progress, elapsed/remaining |
| `get_attributes` | yes | Model, firmware, mainboard ID, capabilities |
| `get_snapshot` | yes | Webcam frame as MCP `Image` content (LLMs see the picture) |
| `discover_printers` | yes | LAN scan |
| `start_print` | only with `--enable-control` | Starts a print from a file already on the printer |
| `pause_print` | only with `--enable-control` | Pauses the current print |
| `resume_print` | only with `--enable-control` | Resumes a paused print |
| `stop_print` | only with `--enable-control` | Stops the current print |

Control tools aren't just gated — they're not *registered* without the
flag, so an LLM that wasn't given the `--enable-control` launch can't see
them in the tool list at all.

## Safety

Printer control actions are gated behind an explicit `enable_control=True`
(library) or `--enable-control` flag (CLI / MCP). Destructive MCP tools
aren't registered when the flag is off, so an LLM never sees them. Still:
leaving a printer running unattended with write-capable agents is your
responsibility, as is securing the HTTP surface (it's unauthenticated — put
a reverse proxy with auth in front if you expose it beyond loopback).

The RTSP bridge itself isn't gated by `--enable-control` because it only
reads the webcam feed and doesn't change printer state; it's enabled
per-server via the `--rtsp` flag.

## Known firmware quirks

- **5 concurrent WebSocket connections max.** The printer's SDCP server
  accepts up to 5 open WebSockets on port 3030; the 6th returns HTTP 500
  with body `"too many client"`. Slots release immediately when a
  connection closes — the CLI each opens and closes one per invocation,
  the HTTP and MCP servers hold a single long-lived one, so this is
  almost never a problem in practice.
- **Paused / errored states don't auto-push Attributes.** The printer only
  sends its `Attributes` frame spontaneously while idle or printing. In
  paused and errored states it stays silent until asked. Since every SDCP
  command needs the printer's `MainboardID`, the client handles this by
  pre-seeding the mainboard ID from a UDP discovery on every connect
  (as of v0.1.1). If you call `Printer.connect()` directly without
  discovery, pass `mainboard_id=` yourself.

## Credits & licensing

- Protocol reference: [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link)
  (Apache-2.0) and [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link).
- `pycentauri` is licensed under Apache-2.0. See [LICENSE](LICENSE).
- Not affiliated with or endorsed by Elegoo.
