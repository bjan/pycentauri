# pycentauri

Python client, CLI, and MCP server for the [Elegoo Centauri
Carbon](https://www.elegoo.com/) 3D printer.

`pycentauri` speaks the printer's native SDCP v3 protocol over its local
WebSocket (port 3030) — no cloud account required. It exposes five surfaces:

1. An **async Python library** for direct integration.
2. A **`centauri` CLI** for quick status checks, snapshots, and control.
3. An **MCP server** so AI agents (Claude Code, Claude Desktop, Cursor, any
   MCP-compatible client) can monitor and drive the printer as a tool.
4. An **HTTP + SSE server** for dashboards, reverse-proxy integration, and
   anything that wants a plain REST API.
5. A **built-in web UI** (dark theme, mobile-friendly) served at `/ui/` by
   the HTTP server — live webcam, progress, temperatures, and control.

> **Status:** alpha. The protocol has been reverse-engineered from the official
> [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) C++ SDK and the
> [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link) project. It
> works against the original Centauri Carbon on current firmware. The newer
> Centauri Carbon 2 (which uses MQTT) is not supported.

## Install

```sh
pip install pycentauri                    # library + CLI
pip install "pycentauri[mcp]"             # + MCP server
pip install "pycentauri[server]"          # + HTTP REST/SSE server
pip install "pycentauri[mcp,server]"      # everything
```

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

# Files on the printer
centauri files --host 192.168.1.209

# Control actions require --enable-control
centauri print start cube.gcode --host 192.168.1.209 --enable-control
centauri print pause              --host 192.168.1.209 --enable-control
centauri print resume             --host 192.168.1.209 --enable-control
centauri print stop               --host 192.168.1.209 --enable-control
```

The host can also come from `PYCENTAURI_HOST` or `~/.config/pycentauri/config.toml`.

## Quick start — Python

```python
import asyncio
from pycentauri import Printer

async def main():
    async with await Printer.connect("192.168.1.209") as printer:
        status = await printer.status()
        print(status.state, status.progress, status.temp_nozzle)

        jpeg = await printer.snapshot()
        with open("shot.jpg", "wb") as f:
            f.write(jpeg)

        async for update in printer.watch():
            print(update.state, update.progress)

asyncio.run(main())
```

## Quick start — HTTP server

```sh
# Read-only, bound to loopback so only this box can hit it:
centauri server --host 192.168.1.209 --port 8787

# Read + write, bound to all interfaces (put a reverse proxy in front):
centauri server --host 192.168.1.209 --bind 0.0.0.0 --port 8787 --enable-control
```

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Redirects to `/ui/` |
| `GET` | `/ui/` | Built-in web dashboard |
| `GET` | `/api/info` | Health + version (JSON) |
| `GET` | `/status` | Latest status push (cached; updates every ~5s) |
| `GET` | `/attributes` | Printer attributes |
| `GET` | `/snapshot` | `image/jpeg` response |
| `GET` | `/stream` | MJPEG stream proxied from the printer (embeds in `<img>`) |
| `GET` | `/discover` | LAN scan |
| `GET` | `/events/status` | Server-Sent Events stream of pushes |
| `POST` | `/print/start` | Body: `{"filename": "cube.gcode"}`. Requires `--enable-control`. |
| `POST` | `/print/{pause,resume,stop}` | Requires `--enable-control`. |

The server holds a single long-lived WebSocket to the printer and reuses
it for every request — no per-request reconnect, and it won't bump into
the firmware's 5-slot limit.

## Quick start — MCP

Register the server with your agent. With Claude Code:

```sh
claude mcp add pycentauri -- python -m pycentauri.mcp
# or, with control actions enabled (gives the agent start/pause/stop/upload):
claude mcp add pycentauri -- python -m pycentauri.mcp --enable-control
```

Set `PYCENTAURI_HOST` in your MCP server env so the agent can't target an
arbitrary IP. The server exposes these tools:

| Tool | Always available | Description |
|---|---|---|
| `get_status` | yes | State, temperatures, progress, elapsed/remaining |
| `get_attributes` | yes | Model, firmware, mainboard ID |
| `list_files` | yes | Files stored on the printer |
| `get_snapshot` | yes | Webcam frame as MCP `Image` content |
| `discover_printers` | yes | LAN scan |
| `start_print` | only with `--enable-control` | Starts a print |
| `pause_print` | only with `--enable-control` | Pauses the current print |
| `resume_print` | only with `--enable-control` | Resumes a paused print |
| `stop_print` | only with `--enable-control` | Stops the current print |
| `upload_file` | only with `--enable-control` | Uploads a file to the printer |

## Safety

Control actions are gated behind an explicit `enable_control=True` (library) or
`--enable-control` flag (CLI / MCP). Destructive MCP tools are not even
registered when the flag is off, so an LLM never sees them. Still: leaving a
printer running unattended with write-capable agents is your responsibility.

## Known firmware quirks

- **5 concurrent WebSocket connections max.** The printer's SDCP server
  accepts up to 5 open WebSockets on port 3030; the 6th returns HTTP 500
  with body `"too many client"`. Slots release immediately when a
  connection closes — the CLI and MCP server each open/close one per
  invocation, so this is almost never a problem in practice.
- **Paused / errored states don't auto-push Attributes.** The printer only
  sends its `Attributes` frame spontaneously while idle or printing. In
  paused and errored states it stays silent until asked. Since every SDCP
  command needs the printer's `MainboardID`, the client takes care of this
  by pre-seeding the mainboard ID from a UDP discovery on every connect
  (as of v0.1.1). If you call `Printer.connect()` directly without
  discovery, pass `mainboard_id=` yourself.

## Credits & licensing

- Protocol reference: [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link)
  (Apache-2.0) and [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link).
- `pycentauri` is licensed under Apache-2.0. See [LICENSE](LICENSE).
- Not affiliated with or endorsed by Elegoo.
