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

CC2 uses different ports and a JSON-RPC discovery probe. This doc is
CC-only.

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
4. The printer starts pushing `Status` frames at that rate.

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
| 324 | `GET_CANVAS_STATUS`     | `{}` | enabled in SDK; not yet exercised by pycentauri |
| 512 | `SUBSCRIBE`             | `{"TimePeriod": <ms>}` | `Ack=0`, then status pushes |

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
| 403  | `SET_TEMPERATURE`   | `{"TempTargetNozzle": <°C>, "TempTargetHotbed": <°C>, "TempTargetBox": <°C>}` |
| 403  | `SET_FAN_SPEED`     | `{"TargetFanSpeed": {"ModelFan": <0-100>, "BoxFan": <0-100>, "AuxiliaryFan": <0-100>}}` |
| 403  | `SET_PRINT_SPEED`   | `{"PrintSpeedPct": <int>}` |
| 403  | `SET_LIGHT`         | (brightness payload) |
| 386  | `VIDEO_STREAM`      | (control payload) |
| 1024 | `LOAD_FILAMENT`     | (TBD) |
| 1025 | `UNLOAD_FILAMENT`   | (TBD) |
| 1043 | `SET_PRINTER_NAME`  | `{"Name": "<str>"}` |

Cmd 403 is overloaded — same code, different payload shapes. If 403
works at all, all four 403 modes likely work (their packet builders
share the same dispatcher in the SDK).

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
