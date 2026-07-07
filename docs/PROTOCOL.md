# SDCP protocol notes (Elegoo Centauri Carbon)

Hard-won field notes on the original Centauri Carbon's local protocol —
the kind of information you wish you had before you started reverse
engineering it. Tested against firmware **V1.1.46** (stock) and
**V0.3.0-o** (OpenCentauri), which share the same `app` binary and are
indistinguishable at the SDCP layer.

> ## ⚠️ DO NOT PROBE UNKNOWN Cmds WHILE A PRINT IS RUNNING
>
> A few unrecognised Cmd codes in quick succession will **crash the
> printer's `app` daemon entirely**, not just close your WebSocket.
>
> On both stock and OpenCentauri, `app` is **the whole printer
> firmware** — host UI, network layer, file management, and the gcode
> interpreter. There is no separate Klipper / Marlin / Moonraker layer
> underneath. When `app` dies, the running print dies with it: motion
> halts mid-layer, the MCU's watchdog cuts heater power, the screen
> goes dark, and your part is destroyed.
>
> **Probe only when the printer is idle (`PrintInfo.Status == 0`) and
> the user has explicitly authorised it.** Even read-only commands count
> if they're not on the confirmed-working list — the firmware doesn't
> distinguish "harmless query" from "destructive action" before
> deciding to RST. Always check status first; if anything is being
> printed, stop and ask.
>
> Recovery if it does crash: power-cycle the printer at the wall. `app`
> re-launches via `/etc/rc.local` at boot. SSH-based hand-restart is
> possible (`setsid nohup /app/app </dev/null >/tmp/app.log 2>&1 &`)
> but a power-cycle is faster and clean.

## Reference projects

- [`ELEGOO-3D/elegoo-link`](https://github.com/ELEGOO-3D/elegoo-link) —
  Elegoo's own C++ SDK, Apache-2.0. The authoritative source for Cmd
  codes, payload schemas, and printer-status enums. Tellingly, the SDK
  has many Cmd-code mappings *commented out* in
  `src/lan/adapters/elegoo_fdm_cc/elegoo_fdm_cc_message_adapter.cpp` —
  see "Probing" below.
- [`CentauriLink/Centauri-Link`](https://github.com/CentauriLink/Centauri-Link)
  — Kivy GUI. `main.py` documents the SDCP envelope and OctoEverywhere
  tunnel layer.
- [`OpenCentauri/cc-fw-tools`](https://github.com/OpenCentauri/cc-fw-tools)
  — community patched firmware. Adds SSH on port 22 and a debug shell
  on 4567, but does not modify the `app` daemon at the SDCP layer.

## Network surface (what the printer exposes)

| Port | Protocol | Use |
|---|---|---|
| 80   | HTTP | Angular web UI (printer's own) |
| 3000 | UDP  | Discovery (broadcast `M99999` → JSON reply) |
| 3030 | WS   | SDCP control: `ws://<ip>:3030/websocket` |
| 3031 | HTTP | MJPEG webcam: `multipart/x-mixed-replace` at `/video` |

The CC2 uses an entirely different transport (MQTT) and has no
broadcast discovery at all — see the **Centauri Carbon 2 protocol
notes** section at the bottom of this document.

## Discovery

Broadcast UDP packet to `255.255.255.255:3000` with literal payload
`M99999` (no JSON, just six ASCII bytes). The printer replies with:

```json
{
  "Id": "<some hex>",
  "Data": {
    "Name":            "Centauri Carbon",
    "MachineName":     "Centauri Carbon",
    "MainboardID":     "48551d180103147000001c0000000000",
    "FirmwareVersion": "V1.1.46"
  }
}
```

The `MainboardID` is the printer's serial number for SDCP routing
purposes — pin it once, use it forever. (`pycentauri.discovery` retries
the probe a few times within the timeout window since UDP can drop.)

## SDCP envelope

Every request from client to printer:

```json
{
  "Id":    "<mainboard id>",
  "Topic": "sdcp/request/<mainboard id>",
  "Data": {
    "Cmd":         <int>,
    "Data":        { ... },
    "RequestID":   "<hex16>",
    "MainboardID": "<mainboard id>",
    "TimeStamp":   <unix ms>,
    "From":        1
  }
}
```

`From: 1` is what Elegoo's own SDK sends. CentauriLink uses `0`; both
appear to work but pycentauri uses `1` to match the official client.

The printer's responses arrive on three topics:

| Topic | Meaning |
|---|---|
| `sdcp/response/<mainboard>` | Direct response to a request, correlated by `RequestID`. The body has `Data.Data.Ack` — `0` = success, anything else = failure. |
| `sdcp/status/<mainboard>` | Push: real-time status (after Cmd 512 subscribe). |
| `sdcp/attributes/<mainboard>` | Push: machine info (auto on connect *while idle/printing*; not while paused or errored). |

## On-connect dance

1. Open WS to `ws://<host>:3030/websocket`.
2. The printer pushes one `Attributes` frame **only if it's idle or
   printing**. In paused / stopped / errored states it stays silent
   until polled. Don't depend on this push.
3. Send `Cmd 512` with `{"TimePeriod": <ms>}` to subscribe to status
   pushes at the requested rate.
4. The printer starts pushing `Status` frames at that rate — **on some
   firmware states it never does** (see "Operational quirks": periodic
   pushes can be permanently dead while `Cmd 0` one-shots still work).

`pycentauri.client.Printer` accepts `mainboard_id=` so it can skip the
implicit Attributes wait — important because the alternative is
deadlocking whenever the printer is paused.

## Cmd codes (CC-firmware)

The official `elegoo-link` SDK lists more Cmd codes than the CC firmware
actually accepts. We have probed and confirmed which work.

### Confirmed-working

| Cmd | Name | Payload | Returns |
|---|---|---|---|
| 0   | `GET_PRINTER_STATUS`    | `{}` | `Ack=0` (the real status arrives separately on the status topic) |
| 1   | `GET_PRINTER_ATTRIBUTES`| `{}` | `Ack=0` (attributes arrive on attributes topic) |
| 128 | `START_PRINT`           | `{Filename, StartLayer, Calibration_switch, PrintPlatformType, Tlp_Switch, slot_map}` | `Ack=0` |
| 129 | `PAUSE_PRINT`           | `{}` | `Ack=0` |
| 130 | `STOP_PRINT`            | `{}` | `Ack=0` |
| 131 | `RESUME_PRINT`          | `{}` | `Ack=0` |
| 324 | `GET_CANVAS_STATUS`     | `{}` | **unprobed** — enabled in the SDK's dispatch table but never exercised against real CC1 firmware. pycentauri implements Canvas on CC2 only (methods 2004/2005). |
| 403 | `CHANGE_PRINT_PARAMS` (speed) | `{"PrintSpeedPct": <int>}` | `Ack=0`; `Status.PrintInfo.PrintSpeedPct` updates on next push |
| 403 | `CHANGE_PRINT_PARAMS` (fans)  | `{"TargetFanSpeed": {"ModelFan": <0..100>, "BoxFan": <0..100>, "AuxiliaryFan": <0..100>}}` | `Ack=0`; fans physically respond within ~1s |
| 403 | `CHANGE_PRINT_PARAMS` (temps) | `{"TempTargetNozzle": <°C>, "TempTargetHotbed": <°C>, "TempTargetBox": <°C>}` | `Ack=0`; heaters engage and `TempTarget*` updates on next push |
| 512 | `SUBSCRIBE`             | `{"TimePeriod": <ms>}` | `Ack=0`, then status pushes |

The three `Cmd 403` payload shapes are dispatched by the firmware based
on which keys are present. You can include only the fields you want to
change — e.g. `{"TargetFanSpeed": {"ModelFan": 50}}` adjusts the model
fan without disturbing the others. Verified live on V0.3.0-o on
2026-06-03 (fan visibly spun, nozzle heated, speed pct reflected on
push). `pycentauri.Printer.set_print_speed/set_fan_speed/set_temperatures`
wrap each variant with safety-bound input validation.

### Confirmed-broken on V1.1.46 *and* V0.3.0-o (OpenCentauri)

The firmware **silently drops the first one or two** of these and then
**crashes the `app` daemon entirely** when more arrive in quick
succession. From the network's point of view this looks like a TCP RST
on the WS connection, but the actual failure mode is much worse:
the daemon process exits, all three SDCP-related ports (80, 3030, 3031)
go silent, and any active print is killed (see the warning at the top
of this doc). Probe new candidates **only on an idle printer**, with
one-Cmd-per-connection plus a `Cmd 0` sanity check after.

| Cmd | Name | Notes |
|---|---|---|
| 258  | `GET_FILE_LIST` (CC code)  | No response, then daemon crash |
| 1044 | `GET_FILE_LIST` (cc2 code) | Same — both Elegoo SDK variants commented out |
| 320  | `PRINT_TASK_LIST`          | Same |
| 1036 | `PRINT_TASK_LIST` (cc2)    | Same |
| 1048 | `GET_DISK_INFO`            | Same |

OpenCentauri did **not** patch the SDCP daemon — it's the same
unmodified Elegoo `app` binary, so the broken-Cmd surface is identical
to stock. SSH on port 22 and a debug shell on port 4567 are the only
new things OpenCentauri exposes; for anything filesystem- or
introspection-related on OpenCentauri printers, prefer SSH over
attempting unsupported SDCP Cmds.

(First tested 2026-04-22 against firmware V1.1.46. Re-probed
2026-05-07 against V0.3.0-o (OpenCentauri); identical results, plus
the daemon-crash mechanism was confirmed via SSH process inspection.)

### Untested but documented in the SDK

The `elegoo_fdm_cc_message_adapter.cpp` source has full packet-building
switch cases for these — they may or may not work on CC firmware. Each
is flagged with `// commented out` in the mapping table:

| Cmd | Name | Payload sketch |
|---|---|---|
| 401  | `MOVE_AXES`         | `{"Axis": "X\|Y\|Z", "Step": <mm>}` |
| 402  | `HOME_AXES`         | `{"Axis": "X\|Y\|Z\|XY\|XYZ"}` |
| 403  | `SET_LIGHT`         | (brightness payload — fourth Cmd 403 variant; the speed/fan/temp variants are confirmed working, see above) |
| 386  | `VIDEO_STREAM`      | (control payload) |
| 1024 | `LOAD_FILAMENT`     | (TBD) |
| 1025 | `UNLOAD_FILAMENT`   | (TBD) |
| 1043 | `SET_PRINTER_NAME`  | `{"Name": "<str>"}` |

When you confirm any of these, update this table with the firmware
version and the date you tested.

## Status payload (Cmd 0 / status push)

Top-level `Data.Status` has both scalars and nested objects. Two
firmware schemas have been seen in the wild:

```jsonc
// V1.1.x (current — scalar temps, separate target keys):
{
  "CurrentStatus":    [1],            // system state — usually [1] (idle/active)
  "TimeLapseStatus":  0,
  "PlatFormType":     0,
  "TempOfHotbed":     50.04,
  "TempOfNozzle":     255.01,
  "TempOfBox":        29.20,
  "TempTargetHotbed": 50,
  "TempTargetNozzle": 255,
  "TempTargetBox":    0,
  "CurrenCoord":      "139.96,123.71,5.33",
  "CurrentFanSpeed":  { "ModelFan": 58, "AuxiliaryFan": 0, "BoxFan": 68 },
  "ZOffset":          0.415,
  "LightStatus":      { "SecondLight": 1, "RgbLight": [0,0,0] },
  "PrintInfo": {
    "Status":          13,            // see PrintStatus codes below
    "CurrentLayer":    34,
    "TotalLayer":      438,
    "CurrentTicks":    364.98,        // seconds elapsed in this job
    "TotalTicks":      4504,          // seconds total
    "Filename":        "ECC_0.4_temperature_tower_PLA0.16_1h15m.gcode",
    "TaskId":          "295cb186-daf5-4b84-9668-59a520e4640a",
    "PrintSpeedPct":   100,
    "Progress":        9
  }
}

// Older (CentauriLink-documented — pair-form temps):
{ "TempOfNozzle": [target, actual], ... }
```

`pycentauri.models.Status._extract_temp` handles both formats. Note the
firmware typo `CurrenStatus` / `CurrenCoord` (not `Current`) — it's
preserved for backward compatibility.

## `PrintInfo.Status` codes

Authoritative table from
`src/lan/adapters/elegoo_fdm_cc/elegoo_fdm_cc_message_adapter.cpp`
lines 33-62. Codes 2–4 and 23–26 are resin-printer / LCD-specific; not
expected on the Carbon but kept for forward-compatibility.

| Code | Name | Notes |
|---|---|---|
| 0 | IDLE | |
| 1 | HOMING | |
| 2 | DROPPING | resin only |
| 3 | EXPOSING | resin only |
| 4 | LIFTING | resin only |
| 5 | PAUSING | transitioning to paused |
| 6 | PAUSED | |
| 7 | STOPPING | transitioning to stopped |
| 8 | STOPPED | |
| 9 | COMPLETED | terminal |
| 10 | FILE_CHECKING | |
| 11 | PRINTER_CHECKING | |
| 12 | RESUMING | not "preparing" — the SDK calls this RESUMING |
| 13 | PRINTING | the meat of every job |
| 14 | ERROR | print stopped due to error |
| 15 | AUTO_LEVELING | |
| 16 | PREHEATING | |
| 17 | RESONANCE_TESTING | |
| 18 | PRINT_START | not "resumed" — see comment for code 12 |
| 19 | AUTO_LEVELING_COMPLETED | |
| 20 | PREHEATING_COMPLETED | the routine `CODE·20` users see between prints |
| 21 | HOMING_COMPLETED | |
| 22 | RESONANCE_TESTING_COMPLETED | |
| 23 | AUTO_FEEDING | resin/LCD |
| 24 | UNLOADING | resin/LCD |
| 25 | UNLOADING_ABNORMAL | resin/LCD |
| 26 | UNLOADING_PAUSED | resin/LCD |

The `pycentauri.models.PrintStatus` class re-exports these as constants;
the web UI maps them to display labels and color classes in
`src/pycentauri/web/app.js`.

## Internal architecture (from SSH inspection on OpenCentauri)

Live process inspection on a printer running OpenCentauri V0.3.0-o
turned up that the proprietary `app` binary is **the entire host-side
firmware**, including a Klipper-derived motion stack that is *embedded*
in `app` rather than running as a separate `klippy` process:

```
/app/app — single ~350MB-resident process serving:
   :80    HTTP web UI
   :3030  SDCP control WebSocket
   :3031  MJPEG webcam HTTP
plus the embedded Klipper stack (clocksync, verify_heater, gcode,
change_filament, virtual_sdcard, etc.) which talks to the stm32 +
strain_gauge_mcu over USB-serial.
```

`ps` shows no `klippy.py` or `moonraker` process; instead the modules
log into `/board-resource/log1` with Klipper's familiar
`[module][line][severity][ts_ms]:body` format. This is why a single
unsupported SDCP Cmd that crashes `app` also kills the active print —
there is no second-tier interpreter underneath to keep the gcode
pipeline alive.

### Useful log signals on OpenCentauri (and likely stock too)

`/board-resource/log1` is plain text and lossy-rotated; tail it for
real-time printer state without going through SDCP. Notable signals
observed on V0.3.0-o:

| Signal | Meaning |
|---|---|
| `[change_filament][...]:ChangeFilament busy : 1` | filament load/unload cycle started |
| `[change_filament][...]:ChangeFilament busy : 0` | filament cycle ended |
| `[app][...]:feed state change : 0 -> 1` | same as `busy : 1` |
| `[app][...]:feed state change : 1 -> 0` | same as `busy : 0` |
| `[gcode][...]:single_command<M729>` | **load-specific** gcode (does not fire during unload) |
| `[gcode][...]:single_command<G1 E120 F240>` | load-direction extrude (positive E) |
| `[gcode][...]:single_command<SET_MIN_EXTRUDE_TEMP S0>` | unload-specific (lowers extrude-temp gate) |
| `[verify_heater][...]:Heater extruder approaching new target of 230.000` | load (230 °C) |
| `[verify_heater][...]:Heater extruder approaching new target of 140.000` | unload (140 °C) |
| `[print_stats][...]>>>>>> current layer changed :: N,report status` | print progressed to layer N |

The OpenCentauri-only [oc-auto-dismiss
sidecar](https://github.com/bjan/oc-auto-dismiss)
(`~/workspace/oc-auto-dismiss/`) uses these signals as its
detection mechanism for auto-dismissing the load-complete dialog.

### Touchscreen (OpenCentauri only — SSH access required)

| Property | Value |
|---|---|
| Device | `/dev/input/event1` |
| Driver | `gt9xxnew_ts` (Goodix) |
| Resolution | 480 × 272 logical |
| Multitouch | Type A (uses `SYN_MT_REPORT`; tracking ID stays at 0 across a held tap, no `-1` lift transition) |

A complete one-finger tap, recorded:

```
EV_KEY  BTN_TOUCH=1
EV_ABS  ABS_MT_POSITION_X
EV_ABS  ABS_MT_POSITION_Y
EV_ABS  ABS_MT_TOUCH_MAJOR=20
EV_ABS  ABS_MT_WIDTH_MAJOR=20
EV_ABS  ABS_MT_TRACKING_ID=0
EV_SYN  SYN_MT_REPORT
EV_SYN  SYN_REPORT
   (held frames, all identical, ~10 ms apart)
EV_KEY  BTN_TOUCH=0
EV_SYN  SYN_REPORT
```

Tap injection works by writing 16-byte `input_event` structs back to
the same device with root privileges; the Goodix driver doesn't
distinguish synthetic from physical taps.

## Operational quirks

- **5 concurrent WebSocket connection limit.** A 6th `connect()` returns
  HTTP 500 with literal body `"too many client"`. Slots release
  immediately on close — there's no cooldown. Confirmed by direct probe
  on 2026-04-22.
- **Unknown Cmd → silent drop, then daemon crash** (not just `RST`).
  See the warning at the top of this doc. The TCP connection going away
  is just the visible symptom of the entire `app` userland daemon
  exiting, which kills any active print. Always check
  `PrintInfo.Status == 0` before sending a command that isn't on the
  confirmed-working list, and one-Cmd-per-connection during research.
- **Paused/errored states don't auto-push Attributes.** The first
  Attributes push only happens in idle or printing states. Pre-seed the
  mainboard ID from discovery if you need to issue commands while
  paused or errored.
- **Some firmware versions include a numeric length prefix on incoming
  WS frames** (e.g. `"123{json...}"`). `pycentauri.sdcp.parse_message`
  strips leading digits before JSON-decoding.
- **Length spikes on the WS reader can crash some `websockets`
  versions.** We pass `max_size=None` on `connect()` to disable the
  default 1 MB frame cap.
- **Discovery probes can drop on busy LANs.** `discover()` retransmits
  the probe a few times within the timeout window.
- **Periodic status pushes (`Cmd 512`) cannot be relied on.** Observed
  live 2026-07-04 on V0.3.0-o: `Cmd 512` is ACKed but **no status
  frames are ever pushed** — on any connection, new or old. Meanwhile
  `Cmd 0` still returns `Ack=0` *and* still emits its one-shot status
  frame on the status topic, ports 80/3031 serve normally, and prints
  are unaffected. **A reboot does not restore pushes**: verified
  2026-07-05 — full `reboot` over SSH, then subscribe-only tests at
  ~1.5 and ~3 minutes post-boot still produced zero pushes while
  `Cmd 0` behaved normally. Clients that passively wait for pushes
  hang forever, so pycentauri sends `Cmd 0` whenever a subscribe
  produces no push within one period (`status()` and `watch()` both do
  this) — treat request-based polling as the primary status path on
  this firmware. **Pushes DO flow mid-print** (verified 2026-07-05:
  ~20 status pushes in 30 s during an active print on the same machine
  that pushes nothing while idle) — the dead scheduler is an
  idle-state phenomenon, and starting a print revives it. The Cmd 0
  fallback simply stops firing once pushes resume, so clients get
  full-rate updates during prints and ~one-per-period polling at idle.
  Remaining open question: whether stock V1.1.46 idles the same way
  (untested since the OpenCentauri flash).

## Camera

- Port 3031, path `/video`, content type
  `multipart/x-mixed-replace; boundary=--foo`. Each MJPEG part is a
  full JPEG (`FF D8 ... FF D9`).
- Single-shot snapshot = open the stream, scan bytes for the SOI
  marker, accumulate until EOI, close the connection.
- ~10 fps native at 640×360. The HTTP server's `/stream` route just
  proxies this stream; the `/snapshot` route is a single-frame grab.

## When to suspect firmware/network rather than the library

If `pycentauri` worked an hour ago and now hangs:

1. `centauri discover` — UDP works regardless of WS state. If this
   fails, the printer is fully offline (rebooting, sleep, power off).
2. `curl http://<host>/` — confirms web UI port 80.
3. `curl http://<host>:3031/video -m 1` — confirms camera.
4. `bash -c 'echo > /dev/tcp/<host>/3030'` — confirms WS port accepts
   TCP.

If **all of 2, 3, and 4 are dead at once** while `centauri discover`
still works (UDP), the `app` daemon has crashed — that's a single
process serving all three TCP ports. Likely cause: an unsupported Cmd
was sent recently. The print, if any, is gone. Power-cycle to recover
(`app` is auto-launched by `/etc/rc.local`). SSH (port 22 on
OpenCentauri, not exposed on stock) lets you confirm the process is
gone via `ps -ef | grep /app/app` and relaunch by hand:

```sh
ssh printer "setsid nohup /app/app </dev/null >/tmp/app.log 2>&1 &"
```

If 1–4 all pass but commands time out: stale connection slot exhaustion
(the gentler failure mode). Wait a few minutes for the firmware's TCP
TIME_WAIT to clean up; if the SDCP server is still wedged after ~5
minutes, power-cycling is the fastest reset.

If 1–4 all pass and *requests* work (`Cmd 0` gets `Ack=0`) but no
status **pushes** ever arrive after a `Cmd 512` subscribe: that's the
dead push scheduler (see "Operational quirks" above). pycentauri
≥ 0.6.0 rides through it automatically by requesting status frames with
`Cmd 0`; older versions hang in `status()`/`watch()` with no known
remedy — a reboot does **not** bring pushes back (verified 2026-07-05).

---

# Centauri Carbon 2 (CC2) protocol notes

The CC2 uses an entirely different transport and command set from the
CC1. These are field notes from probing firmware **01.03.02.51** on
2026-06-30 and 2026-07-03, cross-referenced against the `elegoo-link`
SDK's `elegoo_fdm_cc2_*` adapter source.

## Network surface

| Port | Service | Notes |
|---|---|---|
| 80 | HTTP (Angular SPA + REST) | Same concept as CC1's SPA; also serves `/system/info` for serial number bootstrap |
| 1883 | **MQTT 3.1.1** | Primary control and telemetry transport. Replaces CC1's WebSocket SDCP. |
| 8080 | **MJPEG webcam** | `multipart/x-mixed-replace; boundary=frame`. Serves the stream on **any path** — `/`, `/video`, `/stream` all work. **Unauthenticated** (no access code needed). Confirmed 2026-07-04. |
| 22 | SSH (OpenSSH) | Open by default on stock firmware 01.03.02.51 (unlike CC1, which needs OpenCentauri). Reportedly closed in newer firmware. |
| 3030 | — | **Not present** — no SDCP/WS at all |
| 3031 | — | **Not present** — the webcam moved to :8080 |

## Authentication

MQTT broker on the printer requires username + password:

| Field | Value |
|---|---|
| Username | `elegoo` (literal, hardcoded in SDK) |
| Password | The printer's access code / API key (shown on printer screen, e.g. `Ab3dEf`) |

The HTTP surface uses the same access code as an `X-Token` **query
parameter**: `GET /system/info?X-Token=<code>`. The header form
(`X-Token: <code>`) is rejected with 401 on firmware 01.03.02.51 —
only the query parameter works, despite the Elegoo SDK sending both.

**"LAN Only" mode must be enabled** on the printer (network settings on
the touchscreen) for the local API to be reachable at all. With it off,
the CC2 operates through Elegoo's cloud and leaves port 80 closed — the
serial-number bootstrap then fails with a connection error even though
MQTT :1883 answers. Confirmed required on firmware **02.00.02.00**
(reported by a user 2026-07-06, whose connection started working the
moment they enabled it) and recommended on all firmware. Note that
02.00.02.00 is a lockdown release that also **removes SSH** and blocks
firmware downgrades; if a CC2 on 2.0 still won't talk after enabling LAN
Only, the OpenCentauri project publishes a repacked v01.03.02.51 that
bypasses the downgrade guard.

## MQTT topic structure

All topics are rooted under `elegoo/<serial_number>/`. The serial
number (SN) is obtained from `GET /system/info` (HTTP, authenticated)
or from the method 1001 response.

| Topic pattern | Direction | Purpose |
|---|---|---|
| `elegoo/<sn>/api_register` | client → printer | Registration handshake (required before status pushes) |
| `elegoo/<sn>/<request_id>/register_response` | printer → client | Registration acknowledgement |
| `elegoo/<sn>/<client_id>/api_request` | client → printer | All commands (JSON-RPC style) |
| `elegoo/<sn>/<client_id>/api_response` | printer → client | Command responses (matched by `id`) |
| `elegoo/<sn>/api_status` | printer → broadcast | Unsolicited status pushes (method 6000/6008) |

Subscribing to `elegoo/<sn>/#` captures everything including other
clients' traffic (the MQTT broker doesn't isolate clients from each
other). In practice this means you can observe OrcaSlicer's keepalive
PINGs and any other connected software.

## Connection dance

1. Connect to `tcp://<host>:1883` with `username="elegoo"`,
   `password=<access_code>`, `client_id="1_PC_<random_int>"`.
2. Subscribe to `elegoo/<sn>/#`.
3. Publish registration: `elegoo/<sn>/api_register` with body
   `{"client_id": "<client_id>", "request_id": "<client_id>_req"}`.
4. Receive `register_response` with `{"error": "ok"}`.
5. You are now a registered client. The printer will include your
   `client_id` in broadcast push routing and respond to your requests.

## Request envelope

JSON-RPC-style, published to `elegoo/<sn>/<client_id>/api_request`:

```json
{"id": <int>, "method": <int>, "params": {<method-specific>}}
```

Responses arrive on `elegoo/<sn>/<client_id>/api_response` with the
same `id` echoed back. Broadcast status pushes use method 6000/6008
with auto-incrementing `id` values and arrive on both the
`api_response` topic and the `api_status` topic.

PING/PONG keepalive is separate from JSON-RPC: publish
`{"type": "PING"}`, receive `{"type": "PONG"}`. The Elegoo SDK sends
these every ~30 seconds and **they are mandatory for long-lived
sessions**: without them the printer expires the client's registration
after several quiet minutes and silently stops answering that
session's requests — the MQTT connection stays connected, requests
publish fine, responses just never come back. (Verified 2026-07-05: a
dashboard session went request-deaf after ~6 minutes while a fresh
session got instant answers. An earlier draft of this doc called the
PINGs "unnecessary" based on short-lived sessions — that was wrong.)
pycentauri sends the PING every 30 s from `CC2Printer._ping_loop`.

**Rate limiting:** three or more rapid-fire requests trip a cooldown of
roughly five seconds during which the broker silently drops responses
(the requests are received but never answered). Space requests ≥ 2 s
apart. Observed 2026-07-04 on firmware 01.03.02.51.

## Method codes (CC2 firmware)

### Confirmed-working (probed 2026-07-03, firmware 01.03.02.51)

| Method | Name | Params | Returns |
|---|---|---|---|
| 1001 | `GET_PRINTER_ATTRIBUTES` | `{}` | hostname, model, SN, firmware versions, IP |
| 1002 | `GET_PRINTER_STATUS` | `{}` | Rich status: temps, fans (5 channels), gcode_move (incl. `speed` + `speed_mode`), position, print progress, layer, durations |
| 1020 | `START_PRINT` | `{filename, storage_media, ...}` | (not probed — SDK has it enabled) |
| 1021 | `PAUSE_PRINT` | `{}` | (not probed — SDK has it enabled) |
| 1022 | `STOP_PRINT` | `{}` | (not probed — SDK has it enabled) |
| 1023 | `RESUME_PRINT` | `{}` | `error_code: 1010` when idle (expected), confirmed responsive |
| 1026 | `HOME_AXES` | `{}` | `error_code: 0`, physically homes the axes, triggers 6000 push |
| 1027 | `MOVE_AXES` | `{"axis":"Z","step":<mm>}` | `error_code: 1003` (may require homing first) |
| 1028 | `SET_TEMPERATURE` | `{"extruder": <°C>, "heater_bed": <°C>}` | `error_code: 0` |
| 1029 | `SET_LIGHT` | `{"status": <0\|1>}` | `error_code: 0` |
| 1030 | `SET_FAN_SPEED` | `{"fan": <0-255>, "aux_fan": <0-255>, "box_fan": <0-255>}` | `error_code: 0`, fan physically responds |
| 1031 | `SET_PRINT_SPEED` | `{"mode": <0-3>}` | `error_code: 1010` when idle (expected — only effective mid-print) |
| 1036 | `PRINT_TASK_LIST` | `{}` | `error_code: 0`, array of `history_task_list` with UUIDs, filenames, timestamps, status |
| 1043 | `UPDATE_PRINTER_NAME` | (not probed — SDK has it enabled) | — |
| 2004 | `SET_AUTO_REFILL` | `{"auto_refill": <bool>}` | `error_code: 0`; toggle verified both directions via 2005 read-back (probed 2026-07-04) |
| 2005 | `GET_CANVAS_STATUS` | `{}` | `error_code: 0`; `canvas_info` with `active_canvas_id`, `active_tray_id` (-1 = none), `auto_refill`, and `canvas_list[].tray_list[]` — each tray has `filament_name/type/color/code`, `brand`, `min/max_nozzle_temp`, `status` (1 = loaded), `tray_id` (probed 2026-07-04 with a 4-slot Canvas attached) |

### Not responding (probed 2026-07-03)

| Method | Name | Notes |
|---|---|---|
| 1042 | `VIDEO_STREAM` | No response (timeout). Camera may use a different path. |
| 1044 | `GET_FILE_LIST` | No response (timeout). May need different params or firmware version. |

### Error codes observed

| Code | Meaning (inferred) |
|---|---|
| 0 | Success |
| 1003 | Precondition not met (e.g. axes not homed for MOVE_AXES) |
| 1010 | Invalid state for operation (e.g. RESUME when not printing, SET_SPEED when idle) |

## Status payload (method 1002 response)

Significantly richer than CC1's SDCP status push:

```json
{
  "gcode_move": {
    "speed": 3000,          // ← Commanded speed of the current move, in
                            //   mm/MIN (gcode F word). ÷60 gives the mm/s
                            //   figure the printer's screen shows — verified
                            //   against the screen 2026-07-05. Idle park
                            //   moves report F3000/F6000, purge extrudes
                            //   F150, travels spike to F9000+.
    "speed_mode": 1,        // 0=silent, 1=balanced, 2=sport, 3=ludicrous
    "x": 52.5, "y": 264.0, "z": 80.0,
    "extruder": 0.0
  },
  "extruder": {
    "temperature": 27, "target": 0,
    "filament_detect_enable": 1, "filament_detected": 0
  },
  "heater_bed": { "temperature": 24, "target": 0 },
  "fans": {
    "fan":            { "speed": 0.0 },   // model/part-cooling
    "aux_fan":        { "speed": 0.0 },
    "box_fan":        { "speed": 0.0 },   // chamber
    "controller_fan": { "speed": 0.0 },   // NEW: board cooling
    "heater_fan":     { "speed": 0.0 }    // NEW: hotend heat-break
  },
  "machine_status": {
    "status": 1,            // 1=idle (other values TBD)
    "sub_status": 0,
    "sub_status_reason_code": 0,
    "exception_status": [],
    "progress": 0
  },
  "print_status": {
    "state": "",
    "filename": "",
    "current_layer": 0,
    "print_duration": 0,      // seconds elapsed
    "remaining_time_sec": 0,  // seconds remaining (firmware estimate)
    "total_duration": 0,
    "uuid": "",
    "bed_mesh_detect": true,
    "filament_detect": false,
    "enable": true
  },
  "led": { "status": 1 },
  "tool_head": { "homed_axes": "" },
  "ztemperature_sensor": {
    "temperature": 22,
    "measured_max_temperature": 0,
    "measured_min_temperature": 0
  },
  "external_device": {
    "camera": true,
    "u_disk": true,
    "type": "0303"
  }
}
```

Key differences from CC1's status payload:
- `gcode_move.speed` — the commanded speed of the current move, in
  **mm/min** (the gcode F word). Divide by 60 and it matches the mm/s
  readout on the printer's own screen (verified side-by-side
  2026-07-05). It changes per move, so travels briefly spike it above
  print speeds. (CC1 exposes no speed field at all.)
- `gcode_move.speed_mode` — integer 0–3 directly (CC1 only reports
  `PrintSpeedPct` from which the mode must be inferred). **The firmware
  resets it to 1 (balanced) as part of every Canvas filament-switch
  sequence** (observed 2026-07-05), and only ever to balanced, never to
  another mode. The reset fires a few seconds BEFORE the head parks at
  the chute, while `machine_status` still reports printing — so watching
  the status code alone, the reset looks like a standalone mid-print
  event when it is really the leading edge of a switch. Measured
  reset→park lead times were tight: 6, 7, 7, 8 s (sub-second capture)
  and never above ~9 s across a dozen switches. pycentauri uses that
  gap to tell a firmware reset from a human tapping balanced on the
  touchscreen (byte-identical on the wire): a drop to balanced followed
  by a park within ~12 s is the firmware (re-apply the pinned mode via
  method 1031 when the switch completes); a drop that sits at balanced
  for ~12 s with NO park is a human (release the pin, honor balanced).
  A sustained *non*-balanced mode from the touchscreen is adopted as
  the new pin outright, since the firmware never produces one. Both
  live-verified 2026-07-05.
- Five named fan channels instead of three.
- `remaining_time_sec` is firmware-computed (CC1 requires
  `TotalTicks - CurrentTicks` client-side).
- `filament_detect_enable` / `filament_detected` for runout sensor.

## CC1 vs CC2 at a glance

| | CC1 | CC2 |
|---|---|---|
| Transport | WebSocket :3030 | MQTT :1883 |
| Auth | None | `elegoo` / access_code |
| Discovery | UDP M99999 broadcast | HTTP `/system/info` |
| Cmd code range | 0–512 | 1001–2005+ |
| Envelope | SDCP v3 (`Id`, `Topic`, `Data.Cmd`) | JSON-RPC (`id`, `method`, `params`) |
| Status push | Cmd 512 subscribe → auto-push | Register → method 6000/6008 pushes |
| Live head speed | Not exposed | `gcode_move.speed` (mm/min; ÷60 = screen's mm/s) |
| Fan channels | 3 (model, aux, box) | 5 (+controller, +heater) |
| SSH | OpenCentauri only | Stock |
| Probing risk | **Crashes `app` daemon** | Graceful error_code responses |
