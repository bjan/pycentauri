# pycentauri

Local-network toolkit for [Elegoo Centauri Carbon](https://www.elegoo.com/)
3D printers — the **original Centauri Carbon (CC1)** and the **Centauri
Carbon 2 (CC2)**. One async client, six surfaces: Python library, CLI,
MCP server for AI agents, REST/SSE HTTP server, built-in web dashboard,
and an RTSP bridge for your NVR.

No cloud account, no Elegoo servers — everything talks directly to the
printer on your LAN. `pycentauri` auto-detects which model it's talking
to and speaks the right protocol:

| | CC1 | CC2 |
|---|---|---|
| Transport | SDCP v3 over WebSocket (`:3030`) | JSON-RPC over MQTT (`:1883`) |
| Auth | none | access code (printer screen) |
| Discovery | UDP broadcast | direct IP + HTTP bootstrap |
| Webcam | MJPEG `:3031` | MJPEG `:8080` |
| Live head speed | — | ✓ (`gcode_move.speed` ÷ 60 = the screen's mm/s readout) |
| Fan channels | 3 | 5 |
| Canvas multi-filament | — | ✓ (status + auto-refill) |
| Filament-switch detection | — | ✓ (position-based) |

> **Status:** alpha, but used daily against real printers. Protocols were
> reverse-engineered from Elegoo's official
> [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) C++ SDK, the
> [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link) project,
> and live wire captures. CC1 tested on firmware V1.1.46 and OpenCentauri
> V0.3.0-o; CC2 tested on firmware 01.03.02.51. Full wire-protocol notes
> live in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Install

```sh
pip install pycentauri                    # library + CLI
pip install "pycentauri[mcp]"             # + MCP server
pip install "pycentauri[server]"          # + HTTP REST/SSE server + web UI
pip install "pycentauri[mcp,server]"      # all Python surfaces
```

The RTSP bridge additionally requires
[MediaMTX](https://github.com/bluenviron/mediamtx/releases) and `ffmpeg`
on `$PATH`.

Python 3.10+. Core dependencies: `websockets`, `paho-mqtt`, `httpx`,
`typer`, `pydantic`.

## Connecting to your printer

**CC1** needs only its IP (or nothing at all — it answers UDP discovery).

**CC2** needs its IP *and* its access code, found on the printer's
touchscreen under network/connectivity settings. Pass it as
`--access-code` / `access_code=` / `PYCENTAURI_ACCESS_CODE`. The examples
below use `Ab3dEf` as a stand-in — substitute your own.

> **Enable "LAN Only" mode on the CC2** (network settings on the
> touchscreen). The CC2 gates its local API behind it — with LAN Only
> off the printer works through Elegoo's cloud and leaves the local HTTP
> endpoint closed, so pycentauri can't reach it and you'll get a
> connection error. This is required on firmware 2.0 and recommended on
> all CC2 firmware.

Every CLI command accepts `--host` (env: `PYCENTAURI_HOST`). With no host
given, commands try UDP discovery, which only finds CC1s.

## CLI

```sh
# Discovery (CC1 only — CC2 doesn't answer broadcasts)
centauri discover

# Status, attributes, live watch, snapshot
centauri status     --host 192.168.1.209                        # CC1
centauri status     --host 192.168.1.189 --access-code Ab3dEf   # CC2
centauri status     --host 192.168.1.209 --json
centauri attributes --host 192.168.1.209
centauri watch      --host 192.168.1.209
centauri snapshot   --host 192.168.1.209 shot.jpg

# Print control — all writes require --enable-control
centauri print start cube.gcode --host 192.168.1.209 --enable-control
centauri print pause            --host 192.168.1.209 --enable-control
centauri print resume           --host 192.168.1.209 --enable-control
centauri print stop             --host 192.168.1.209 --enable-control

# Live adjust while printing
centauri speed sport                            --host 192.168.1.209 --enable-control
centauri fan  --model 100 --aux 60 --chamber 30 --host 192.168.1.209 --enable-control
centauri temp --nozzle 215 --bed 60             --host 192.168.1.209 --enable-control

# Canvas multi-filament (CC2 only)
centauri canvas       --host 192.168.1.189 --access-code Ab3dEf
centauri refill --on  --host 192.168.1.189 --access-code Ab3dEf --enable-control
```

`centauri canvas` prints each tray's filament, color, temperature range,
and loaded state:

```
auto_refill  : OFF
active_tray  : none
connected    : yes

canvas #0:
  ● tray 0: PLA Wood (PLA) #F72221 [190-230°C]
  ● tray 1: PLA Wood (PLA) #AF7832 [190-230°C]
  ● tray 2: PETG (PETG) #A03BF7 [230-260°C]
  ● tray 3: PLA Wood (PLA) #D2C5A3 [190-230°C]
```

### Speed modes

Both printers accept exactly four speed settings — arbitrary percentages
are silently ignored by the firmware:

| Mode | CC1 wire value (`PrintSpeedPct`) | CC2 wire value (`speed_mode`) |
|---|---|---|
| `silent` | 50 | 0 |
| `balanced` | 100 | 1 |
| `sport` | 130 | 2 |
| `ludicrous` | 160 | 3 |

Speed changes only take effect while a print is actively running.

#### CC2 speed pinning (the firmware fights you, so pycentauri fights back)

The CC2 firmware **resets the speed mode back to balanced on every
Canvas filament switch.** On a multi-color print that switch happens
every couple of minutes, so set sport once and the printer quietly
drops you to balanced at the next color change. This is firmware
behavior; it happens whether you set the speed from pycentauri or from
the printer's own touchscreen.

pycentauri works around it with **pin-and-enforce** (CC2 only, requires
`--enable-control`):

- **Whatever mode you select gets pinned.** Set it via pycentauri (pins
  immediately) or on the touchscreen (pinned after it holds ~8 seconds —
  the firmware never *resets* to a non-balanced mode, so a sustained
  sport/ludicrous/silent must be a human).
- **A firmware reset is re-applied.** The reset fires a few seconds
  *before* the head parks at the chute, so when the speed drops to
  balanced pycentauri waits to see what follows: a filament switch
  (the head parking) within ~12 seconds means it was the firmware, and
  the pinned mode is re-applied as soon as the switch completes.
- **A human's balanced is honored.** If the speed drops to balanced and
  *no* switch follows within ~12 seconds, that was you tapping balanced
  on the touchscreen — the pin is released and balanced stays. (On the
  wire a human tap is byte-identical to a firmware reset; the presence
  or absence of the following switch is what tells them apart. Measured
  reset→park lead times cluster at 6–8 s, well inside the window.)
- **The pin clears when the print ends.**

So both paths just work: set any speed from either the app or the
printer and it sticks across filament switches, and you can always
drop back to balanced from the touchscreen whenever you want.

The CC1 has none of this — its speed mode stays where you put it, so
`set_print_speed` is a plain one-shot there.

## Python library

```python
import asyncio
from pycentauri import Printer, CC2Printer, connect_auto

async def main():
    # Explicit CC1
    async with await Printer.connect("192.168.1.209") as printer:
        st = await printer.status()
        print(st.print_status, st.progress, st.temp_nozzle)

    # Explicit CC2
    async with await CC2Printer.connect("192.168.1.189", access_code="Ab3dEf") as printer:
        st = await printer.status()
        print(st.temp_nozzle, st.raw["_cc2"]["gcode_move_speed"])  # mm/min; ÷60 = screen's mm/s

        canvas = await printer.canvas_status()
        for unit in canvas.canvas_list:
            for tray in unit.tray_list:
                print(tray.tray_id, tray.filament_name, tray.filament_color)

    # Auto-detect — port-probes :3030 vs :1883 and returns the right class
    async with await connect_auto("192.168.1.189", access_code="Ab3dEf") as printer:
        attrs = await printer.attributes()
        print(attrs.machine_name, attrs.firmware_version)

asyncio.run(main())
```

Both classes expose the same API: `status()`, `attributes()`, `watch()`
(async iterator of live status), `snapshot()`, `start_print()`, `pause()`,
`resume()`, `stop()`, `set_print_speed()`, `set_fan_speed()`,
`set_temperatures()`, `canvas_status()`, `set_auto_refill()`. Write
methods require `enable_control=True` at connect time and raise
`ControlDisabledError` otherwise. Canvas methods raise `PrinterError` on
CC1 (no Canvas support over SDCP).

CC2-only telemetry rides along in `Status.raw["_cc2"]`: the live head
speed (`gcode_move_speed` — the commanded speed of the current move in
**mm/min**; divide by 60 for the mm/s figure the printer's screen
shows), `speed_mode`, filament runout sensor state,
firmware-computed `remaining_time_sec`, `machine_status`/`sub_status`
raw codes, and `external_device` (camera / U-disk presence).

## HTTP server + web UI

```sh
# Read-only, loopback only
centauri server --host 192.168.1.209

# Read + write + RTSP, on the LAN (put an authenticating proxy in front)
centauri server --host 192.168.1.209 --bind 0.0.0.0 --port 8787 \
                --enable-control --rtsp

# CC2
centauri server --host 192.168.1.189 --access-code Ab3dEf \
                --bind 0.0.0.0 --port 8787 --enable-control
```

The server holds a single long-lived connection to the printer
(WebSocket for CC1, MQTT for CC2) with automatic reconnect and
exponential backoff — it will never exhaust CC1's 5-connection limit.

The **web UI** at `/ui/` is a clean dark dashboard, mobile-friendly and
dependency-free (no CDN assets — works on an air-gapped LAN): live
webcam, job progress with layer/ETA, printer state, thermals, kinematics
(with live head speed on CC2), pause/resume/stop, speed-mode selector,
fan and heater sliders that hydrate from live values, a Canvas panel
with per-tray color swatches and an auto-refill toggle (CC2), and RTSP
bridge controls. Control panels only render when the server was started
with `--enable-control`.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| `GET` | `/` | Redirects to `/ui/` |
| `GET` | `/ui/` | Web dashboard |
| `GET` | `/api/info` | Health + version + connection state |
| `GET` | `/status` | Latest status (typed summary + full `raw` payload) |
| `GET` | `/attributes` | Model, firmware, mainboard ID |
| `GET` | `/snapshot` | Single JPEG frame |
| `GET` | `/stream` | MJPEG proxy (drop into an `<img>` tag) |
| `GET` | `/events/status` | Server-Sent Events stream of status pushes |
| `GET` | `/discover` | UDP LAN scan (finds CC1s) |
| `GET` | `/canvas` | Canvas state (CC2; `501` on CC1) |
| `GET` | `/docs`, `/redoc` | OpenAPI documentation |
| `POST` | `/print/start` | `{"filename": "cube.gcode", "storage": "local"}` † |
| `POST` | `/print/pause` · `/print/resume` · `/print/stop` | † |
| `POST` | `/print/speed` | `{"mode": "sport"}` or `{"mode": 130}` † |
| `POST` | `/print/fan` | `{"model": 50, "auxiliary": 30, "chamber": 0}` — any subset, 0–100 † |
| `POST` | `/print/temperature` | `{"nozzle": 215, "bed": 60}` — any subset, °C, 0 = off † |
| `POST` | `/canvas/refill` | `{"enabled": true}` (CC2) † |
| `GET` | `/api/rtsp` | RTSP bridge state (when `--rtsp`) |
| `POST` | `/api/rtsp/start` · `/api/rtsp/stop` | Toggle the bridge (when `--rtsp`) |

† requires the server to be launched with `--enable-control`; otherwise
the route isn't registered at all.

Temperature writes are bounds-checked server-side: nozzle 0–300 °C,
bed 0–110 °C, chamber 0–60 °C.

## MCP server (AI agents)

Give Claude Code, Claude Desktop, Cursor, or any MCP client eyes and
hands on your printer:

```sh
# Read-only (status, snapshot, attributes, discovery, canvas)
claude mcp add pycentauri --env PYCENTAURI_HOST=192.168.1.209 \
    -- python -m pycentauri.mcp

# With control tools
claude mcp add pycentauri-cc2 \
    --env PYCENTAURI_HOST=192.168.1.189 \
    --env PYCENTAURI_ACCESS_CODE=Ab3dEf \
    -- python -m pycentauri.mcp --enable-control
```

The target host is pinned in the server's environment at spawn time — a
prompt-injected agent cannot redirect commands to an arbitrary IP,
because no tool takes a host parameter.

| Tool | Availability | Description |
|---|---|---|
| `get_status` | always | State, temps, progress, layer, position, fans |
| `get_attributes` | always | Model, firmware, mainboard ID |
| `get_snapshot` | always | Webcam frame as MCP image — the model *sees* the print |
| `discover_printers` | always | UDP LAN scan |
| `get_canvas_status` | always | Canvas trays, colors, auto-refill (CC2) |
| `start_print` | `--enable-control` | Start a file already on the printer |
| `pause_print` / `resume_print` / `stop_print` | `--enable-control` | Job control |
| `set_print_speed` | `--enable-control` | `silent`/`balanced`/`sport`/`ludicrous` |
| `set_fan_speed` | `--enable-control` | Any subset of model/aux/chamber, 0–100% |
| `set_temperatures` | `--enable-control` | Any subset of nozzle/bed/chamber, °C |
| `set_auto_refill` | `--enable-control` | Canvas auto-refill toggle (CC2) |

Control tools aren't merely gated — without the flag they are never
registered, so they don't appear in the model's tool list at all.

## RTSP bridge

Re-streams the printer's MJPEG webcam as H.264/RTSP for clients that
don't speak MJPEG — Home Assistant, Frigate, Jellyfin, Synology
Surveillance, VLC:

```sh
# Standalone (foreground, Ctrl-C to stop)
centauri rtsp --host 192.168.1.209
# → rtsp://<this-host>:8554/printer

# Integrated with the HTTP server — adds a STREAM panel to the web UI
centauri server --host 192.168.1.209 --rtsp --bind 0.0.0.0
```

MediaMTX only runs the ffmpeg transcode while a client is connected, so
idle cost is zero. Tunables: `--fps`, `--bitrate`, `--preset`, `--path`,
`--port` (standalone) or the `--rtsp-*` variants on `centauri server`.
The bridge picks the correct camera port for CC1 vs CC2 automatically.

## Print status codes

`print_status` in the API and library uses the CC1 firmware's code
space, extended with three codes for CC2 Canvas operations:

| Code | Meaning | | Code | Meaning |
|---|---|---|---|---|
| 0 | Idle | | 13 | Printing |
| 1 | Homing | | 14 | Error |
| 5 | Pausing | | 15 | Leveling |
| 6 | Paused | | 16 | Preheating |
| 7 | Stopping | | **27** | **Switching filament** (CC2) |
| 8 | Stopped | | **28** | **Filament load complete** (CC2) |
| 9 | Completed | | **29** | **Unloading filament** (CC2) |

The full table (including CC1's resin-inherited codes) is in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

On the CC2, mid-print Canvas filament switches are detected by head
position: the firmware never fully leaves its "printing" state during a
switch, but the head parks at the purge chute behind the bed (y ≥ 258 mm,
physically outside the printable area) for the duration. pycentauri
reports code 27 the entire time the head is parked there mid-print.

## Safety model

- **Explicit opt-in for writes.** Every surface requires
  `enable_control=True` / `--enable-control` before any state-changing
  command is possible. Read-only is the default everywhere.
- **Bounds-checked heaters.** The library refuses temperature targets
  outside nozzle 0–300 °C / bed 0–110 °C / chamber 0–60 °C even though
  the firmware might accept them.
- **Unauthenticated HTTP surface.** The REST server has no auth of its
  own. Bind it to loopback (the default) or put an authenticating
  reverse proxy in front before exposing it beyond localhost.
- **You own unattended printing.** An agent with control tools can pause,
  stop, or heat your printer. Leaving one unattended is your call.

## Known firmware quirks

### CC1 (original Centauri Carbon)

- **5 concurrent WebSocket slots, hard.** The 6th connection gets HTTP
  500 `"too many client"`. Slots free on close. The CLI opens one per
  invocation; the HTTP/MCP servers hold exactly one long-lived slot.
- **Paused/errored states don't push Attributes.** Every SDCP command
  needs the printer's `MainboardID`, which normally arrives in an
  Attributes push — but not while paused or errored. pycentauri
  pre-seeds it from UDP discovery on every connect. If you call
  `Printer.connect()` on a paused printer without discovery, pass
  `mainboard_id=` yourself.
- **Unknown commands crash the firmware.** A few unrecognised SDCP
  commands in quick succession kill the printer's `app` daemon — and any
  active print with it. Don't probe undocumented command codes against a
  printer that's doing something you care about.
- **The push scheduler goes dormant at idle (and a reboot doesn't wake
  it).** The firmware can enter a state where `Cmd 512` subscribes are
  acknowledged but no status frame is ever pushed while the printer
  sits idle — persisting across reboots (verified 2026-07-05 on
  V0.3.0-o). Starting a print revives pushes at full rate. One-shot
  `Cmd 0` requests always work, so pycentauri automatically falls back
  to polling when a subscribe goes quiet (~7 s updates at idle,
  full-rate pushes while printing). Clients that rely purely on
  subscribe pushes will hang forever on an idle printer in this state.

### CC2 (Centauri Carbon 2)

- **No UDP discovery.** The CC2 ignores broadcast probes; specify its IP
  explicitly. Give it a static DHCP lease — it doesn't register a
  hostname with most routers, so its address drifts otherwise.
- **Access code required for MQTT**, passed as the password with
  username `elegoo`. The HTTP bootstrap (`/system/info`) wants the same
  code as an `X-Token` *query parameter* — it ignores the header form.
- **The webcam is unauthenticated.** MJPEG on `:8080` (any path) is open
  to anyone on your LAN, access code or not. That's the firmware's
  choice, not ours.
- **Rate limiting.** Rapid-fire MQTT requests (3+ back-to-back) trip a
  cooldown of a few seconds during which the broker silently drops
  responses. pycentauri's polling cadence stays under it; your scripts
  should too.
- **The firmware resets the speed mode to balanced on every Canvas
  filament switch.** pycentauri pins your chosen mode and re-applies it
  automatically, while still honoring a deliberate balanced from the
  touchscreen — see
  [CC2 speed pinning](#cc2-speed-pinning-the-firmware-fights-you-so-pycentauri-fights-back)
  above for how it tells the two apart.
- **Registrations expire without an app-level PING.** The printer
  forgets a registered client after several quiet minutes and silently
  stops answering that session's requests — the MQTT connection itself
  stays up, so there's no error to catch. pycentauri sends the SDK's
  `{"type": "PING"}` keepalive every 30 s to hold the registration; if
  you write your own client, you must too.
- **File list (method 1044) and video stream (1042) don't respond** on
  firmware 01.03.02.51, so remote print-start on CC2 requires knowing
  the filename in advance.

## Project layout & docs

```
src/pycentauri/
├── client.py      # CC1: async SDCP-over-WebSocket client
├── cc2.py         # CC2: async JSON-RPC-over-MQTT client (same API)
├── connect.py     # connect_auto() — port-probe model detection
├── sdcp.py        # SDCP v3 envelope build/parse
├── discovery.py   # UDP broadcast discovery
├── camera.py      # MJPEG frame grabber
├── models.py      # Status / Attributes / CanvasStatus / PrintInfo
├── cli.py         # Typer CLI
├── server.py      # FastAPI app + connection supervisor
├── rtsp.py        # MediaMTX/ffmpeg bridge
├── mcp/           # FastMCP stdio server
└── web/           # Static dashboard (no build step, no CDN)
```

- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — both wire protocols in
  detail: envelopes, command/method tables with tested-on dates, status
  payloads, error codes, failure modes, and a CC1-vs-CC2 comparison.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module map and
  request flow.

## Development

```sh
git clone https://github.com/bjan/pycentauri && cd pycentauri
python -m venv .venv && .venv/bin/pip install -e ".[mcp,server,dev]"

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy src          # strict mode
.venv/bin/pytest -q         # no printer required — tests use in-process fakes
```

Tests run against an in-process fake SDCP WebSocket server plus pure
translation-layer tests for CC2; nothing in CI touches real hardware.
Live verification against a physical printer is manual — `centauri
status`, `centauri canvas`, and a fan write are the standard smoke
test after protocol-layer changes.

## Credits & license

- Protocol references: Elegoo's
  [`elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) SDK
  (Apache-2.0) and
  [`CentauriLink`](https://github.com/CentauriLink/Centauri-Link).
- Licensed under Apache-2.0 — see [LICENSE](LICENSE).
- Not affiliated with or endorsed by Elegoo. Reverse-engineered
  protocols can break with any firmware update; nothing here is
  warranted to keep your prints alive.
