"""Async client for the Elegoo Centauri Carbon 2 (MQTT transport).

The CC2 uses MQTT on port 1883 instead of the CC1's WebSocket SDCP on
3030. This module provides ``CC2Printer``, a subclass of ``Printer``
that speaks the CC2 protocol while exposing the same public API — the
rest of pycentauri (CLI, server, MCP, web UI) works unchanged.

Auth: ``username="elegoo"`` + the printer's access code as password.
Topics: ``elegoo/<serial_number>/<client_id>/api_request`` for commands,
``elegoo/<serial_number>/<client_id>/api_response`` for replies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import paho.mqtt.client as mqtt
from typing_extensions import Self

from pycentauri import camera as camera_module
from pycentauri import sdcp
from pycentauri.client import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
    Printer,
    PrinterError,
    RequestTimeoutError,
)
from pycentauri.models import Attributes, CanvasStatus, Status

log = logging.getLogger(__name__)

MQTT_PORT = 1883
CC2_USERNAME = "elegoo"
PING_INTERVAL_S = 30.0
# Speed-mode pinning. The firmware only ever resets speed_mode TO balanced
# (1), as part of every Canvas filament switch — the reset fires several
# seconds before the head parks (T-6..10s observed 2026-07-05) while the
# status still reads "printing", and its lead time varies. A snapshot-on-
# switch design lost that arms race (a reset leading its switch by more than
# the debounce window poisoned the baseline), so instead the mode a user
# selects is PINNED and enforced regardless of why it drifted: a
# disagreement lasting ENFORCE_AFTER_S while printing gets re-applied, at
# most once per ENFORCE_MIN_INTERVAL_S. A sustained non-balanced mode from
# the touchscreen (held PIN_LEARN_S) adopts the pin, since the firmware
# never resets to a non-balanced mode.
# PIN_LEARN_S must be SHORTER than ENFORCE_AFTER_S: a touchscreen change to
# a non-balanced mode has to be adopted as the new pin before enforcement
# would revert it, or screen users could never override a pinned mode.
PIN_LEARN_S = 8.0
ENFORCE_AFTER_S = 12.0
ENFORCE_MIN_INTERVAL_S = 30.0
# Lifecycle commands (start/pause/stop/resume) are answered only after the
# firmware finishes the mechanical sequence — a resume reheats and unparks
# before responding, easily exceeding the default 15 s request timeout
# (observed 2026-07-05: resume succeeded but its response arrived late,
# surfacing a false error). Give them a much longer leash.
CONTROL_TIMEOUT_S = 90.0


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """Merge ``patch`` into ``base`` in-place, recursing into nested dicts."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _cc2_status_to_cc1(result: dict[str, Any]) -> dict[str, Any]:
    """Translate a CC2 method-1002 result into CC1-shaped Status payload.

    The ``Status.from_payload`` factory expects CC1 PascalCase keys. We
    map the CC2 payload so one ``Status`` class serves both printers.
    """
    gm = result.get("gcode_move", {})
    ext = result.get("extruder", {})
    bed = result.get("heater_bed", {})
    z_sens = result.get("ztemperature_sensor", {})
    fans_raw = result.get("fans", {})
    ms = result.get("machine_status", {})
    ps = result.get("print_status", {})
    led = result.get("led", {})

    fan_map = {
        "ModelFan": int(fans_raw.get("fan", {}).get("speed", 0)),
        "AuxiliaryFan": int(fans_raw.get("aux_fan", {}).get("speed", 0)),
        "BoxFan": int(fans_raw.get("box_fan", {}).get("speed", 0)),
        "ControllerFan": int(fans_raw.get("controller_fan", {}).get("speed", 0)),
        "HeaterFan": int(fans_raw.get("heater_fan", {}).get("speed", 0)),
    }

    speed_mode_to_pct: dict[int, int] = {0: 50, 1: 100, 2: 130, 3: 160}
    speed_pct = speed_mode_to_pct.get(gm.get("speed_mode", 1), 100)

    x = gm.get("x", 0.0)
    y = gm.get("y", 0.0)
    z = gm.get("z", 0.0)

    status_code = ms.get("status", 0)
    print_status = _cc2_machine_status_to_print_status(
        status_code,
        ms.get("sub_status", 0),
        head_y=y,
        progress=ms.get("progress"),
    )

    return {
        "CurrentStatus": [status_code],
        "TempOfNozzle": ext.get("temperature"),
        "TempTargetNozzle": ext.get("target"),
        "TempOfHotbed": bed.get("temperature"),
        "TempTargetHotbed": bed.get("target"),
        "TempOfBox": z_sens.get("temperature"),
        "TempTargetBox": 0,
        "CurrenCoord": f"{x},{y},{z}",
        "CurrentFanSpeed": fan_map,
        "ZOffset": 0,
        "LightStatus": {"SecondLight": led.get("status", 0)},
        "TimeLapseStatus": 0,
        "PlatFormType": 0,
        "PrintInfo": {
            "Status": print_status,
            "Filename": ps.get("filename") or "",
            "CurrentLayer": ps.get("current_layer"),
            "TotalLayer": None,
            "CurrentTicks": ps.get("print_duration"),
            "TotalTicks": (ps.get("print_duration", 0) or 0)
            + (ps.get("remaining_time_sec", 0) or 0),
            "Progress": ms.get("progress", 0),
            "PrintSpeedPct": speed_pct,
            "TaskId": ps.get("uuid"),
        },
        # CC2-only extras preserved in raw for the web UI / callers that
        # want the richer data:
        "_cc2": {
            "gcode_move_speed": gm.get("speed"),
            "speed_mode": gm.get("speed_mode"),
            "machine_status": status_code,
            "sub_status": ms.get("sub_status", 0),
            "sub_status_reason_code": ms.get("sub_status_reason_code", 0),
            "filament_detected": ext.get("filament_detected"),
            "filament_detect_enable": ext.get("filament_detect_enable"),
            "remaining_time_sec": ps.get("remaining_time_sec"),
            "exception_status": ms.get("exception_status", []),
            "homed_axes": result.get("tool_head", {}).get("homed_axes", ""),
            "external_device": result.get("external_device", {}),
        },
    }


# The bed is 256 mm deep. The Canvas park/purge chute sits behind it at
# y=264 — a coordinate unreachable while actually printing. A head parked
# past this line during an active print is doing a filament operation.
# (Live capture 2026-07-04: during a Canvas mid-print switch the firmware
# stays machine_status=2 / sub_status=2075 the entire time, with only
# sub-second sub_status blips, so position is the only continuous signal.)
_PURGE_ZONE_Y = 258.0


def _cc2_machine_status_to_print_status(
    status: int,
    sub_status: int,
    *,
    head_y: float | None = None,
    progress: int | None = None,
) -> int:
    """Map CC2's machine_status + sub_status to CC1's PrintInfo.Status codes.

    CC2 uses a two-level state machine; CC1 uses a flat enum. This merges
    both levels into the CC1 code space, adding codes 27-29 for CC2-only
    states that have no CC1 equivalent.
    """
    if status == 0:
        return 0  # initializing → idle
    if status == 1:
        return 0  # idle
    if status == 2:
        if sub_status == 2801 or sub_status == 2802:
            return 1  # homing
        if sub_status == 2901 or sub_status == 2902:
            return 15  # auto leveling
        if sub_status == 2501:
            return 5  # pausing
        if sub_status in (2502, 2505):
            return 6  # paused
        if sub_status == 2401:
            return 12  # resuming
        if sub_status == 2503:
            return 7  # stopping
        if sub_status == 2504:
            return 8  # stopped
        if sub_status == 2077:
            return 9  # completed
        # Parked at the purge chute mid-print → filament switch. The
        # progress guard keeps the end-of-print park (same XY, progress
        # 100) from reading as a switch during the status=2 → 1 handoff.
        if head_y is not None and head_y >= _PURGE_ZONE_Y and (progress is None or progress < 100):
            return 27
        if sub_status in (1045, 1096, 1405, 1906):
            return 16  # preheating (nozzle or bed)
        return 13  # printing (default for status=2)
    if status in (3, 4):
        # FILAMENT_OPERATING — canvas filament switch or manual load/unload
        if sub_status in (1133, 1134, 1135):
            return 27  # filament loading (CC2-only code)
        if sub_status == 1136:
            return 28  # filament load complete
        if sub_status in (1144, 1145):
            return 29  # filament unloading
        return 27  # generic filament operation
    if status == 5:
        return 15  # auto leveling
    if status == 6:
        return 14  # error
    # Unknown machine_status from a future firmware — return an unmapped
    # code (renders as "CODE·99" in the UI) rather than a raw value that
    # could collide with a real CC1 code and mislabel the state.
    return 99


def _cc2_attrs_to_cc1(result: dict[str, Any]) -> dict[str, Any]:
    """Translate a CC2 method-1001 result into CC1-shaped Attributes payload."""
    sv = result.get("software_version", {})
    return {
        "MainboardID": result.get("sn", ""),
        "Name": result.get("hostname", ""),
        "MachineName": result.get("machine_model", result.get("hostname", "")),
        "FirmwareVersion": sv.get("ota_version", ""),
        "ProtocolVersion": result.get("protocol_version", ""),
    }


class CC2Printer(Printer):
    """Async client for the Centauri Carbon 2 (MQTT transport).

    Drop-in replacement for ``Printer`` (CC1). Use
    ``pycentauri.connect_auto()`` to auto-detect CC1 vs CC2, or call
    ``CC2Printer.connect()`` directly if you know the target is a CC2.
    """

    PRINT_SPEED_MODES: ClassVar[dict[str, int]] = {
        "silent": 0,
        "balanced": 1,
        "sport": 2,
        "ludicrous": 3,
    }

    def __init__(
        self,
        host: str,
        *,
        enable_control: bool = False,
        access_code: str = "",
        serial_number: str | None = None,
        push_period_ms: int = 5000,
        mainboard_id: str | None = None,
    ) -> None:
        super().__init__(
            host,
            enable_control=enable_control,
            push_period_ms=push_period_ms,
            mainboard_id=mainboard_id,
        )
        self.access_code = access_code
        self._serial_number = serial_number or ""
        self._client_id = f"1_PC_{random.randint(10**12, 10**14)}"
        self._request_id_prefix = f"{self._client_id}_req"
        self._mqtt: mqtt.Client | None = None
        self._mqtt_connected = asyncio.Event()
        self._registered = asyncio.Event()
        self._req_counter = 0
        self._pending_mqtt: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_full_result: dict[str, Any] | None = None
        self._connect_error: str | None = None
        self._ping_task: asyncio.Task[None] | None = None
        # Speed-mode pinning (see module constants for the rationale).
        self._pinned_mode: int | None = None
        self._pin_candidate: int | None = None
        self._pin_candidate_since: float = 0.0
        self._mismatch_since: float | None = None
        self._last_enforce: float = float("-inf")
        self._enforce_task: asyncio.Task[None] | None = None
        self._now = time.monotonic  # overridable for tests

    @classmethod
    async def connect(
        cls,
        host: str,
        *,
        enable_control: bool = False,
        access_code: str = "",
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        mainboard_id: str | None = None,
        push_period_ms: int = 5000,
        serial_number: str | None = None,
    ) -> Self:
        """Connect to a CC2 printer via MQTT.

        ``access_code`` is the printer's API key / password (shown on
        the printer screen, e.g. ``"Ab3dEf"``). ``serial_number`` is
        obtained from HTTP ``/system/info`` or method 1001; if omitted
        we fetch it automatically.
        """
        if not access_code:
            raise PrinterError(
                "CC2Printer.connect() requires access_code (the printer's "
                "API key, shown on its screen). Pass --access-code or set "
                "PYCENTAURI_ACCESS_CODE."
            )
        if not serial_number:
            serial_number = await _fetch_serial(host, access_code, connect_timeout)
        self = cls(
            host,
            enable_control=enable_control,
            access_code=access_code,
            serial_number=serial_number,
            push_period_ms=push_period_ms,
            mainboard_id=mainboard_id or serial_number,
        )
        self._loop = asyncio.get_running_loop()
        self._setup_mqtt()
        self._mqtt.connect_async(host, MQTT_PORT, keepalive=60)  # type: ignore[union-attr]
        self._mqtt.loop_start()  # type: ignore[union-attr]
        try:
            await asyncio.wait_for(self._mqtt_connected.wait(), timeout=connect_timeout)
        except asyncio.TimeoutError as err:
            self._mqtt.loop_stop()  # type: ignore[union-attr]
            raise PrinterError(f"MQTT connect to {host}:{MQTT_PORT} timed out") from err
        if self._connect_error is not None:
            self._mqtt.disconnect()  # type: ignore[union-attr]
            self._mqtt.loop_stop()  # type: ignore[union-attr]
            raise PrinterError(
                f"MQTT connection refused: {self._connect_error}. "
                "Check the access code (shown on the printer's screen)."
            )
        # Registration is published from _on_connect (so reconnects
        # re-register too) — here we just wait for the acknowledgement.
        try:
            await asyncio.wait_for(self._registered.wait(), timeout=connect_timeout)
        except asyncio.TimeoutError as err:
            self._mqtt.disconnect()  # type: ignore[union-attr]
            self._mqtt.loop_stop()  # type: ignore[union-attr]
            raise PrinterError("CC2 registration handshake timed out") from err
        # App-level keepalive. The Elegoo SDK sends {"type": "PING"} every
        # ~30 s and this is NOT optional: the printer expires a client's
        # registration after several quiet minutes, at which point it
        # silently stops answering that session's requests (observed
        # 2026-07-05 — a dashboard session went request-deaf after ~6 min
        # while a fresh session worked instantly). MQTT-level keepalive
        # does not count; only the app-level PING keeps registration alive.
        self._ping_task = asyncio.create_task(self._ping_loop(), name=f"pycentauri-cc2-ping-{host}")
        log.info("CC2 connected: %s (sn=%s)", host, serial_number)
        return self

    async def _ping_loop(self) -> None:
        topic = f"elegoo/{self._serial_number}/{self._client_id}/api_request"
        while not self._closed:
            await asyncio.sleep(PING_INTERVAL_S)
            if self._mqtt is not None:
                self._mqtt.publish(topic, '{"type": "PING"}', qos=0)

    # --- public API overrides --------------------------------------------------

    @property
    def camera_port(self) -> int:
        return camera_module.CAMERA_PORT_CC2

    async def status(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Status:
        result = await self._cc2_request(1002, {}, timeout=timeout)
        self._last_full_result = result
        payload = _cc2_status_to_cc1(result)
        self._track_speed_mode(payload)
        st = Status.from_payload(payload)
        self._latest_status = st
        self._latest_status_event.set()
        return st

    async def attributes(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Attributes:
        if self._latest_attributes is not None:
            return self._latest_attributes
        result = await self._cc2_request(1001, {}, timeout=timeout)
        payload = _cc2_attrs_to_cc1(result)
        attrs = Attributes.from_payload(payload)
        self._latest_attributes = attrs
        self._latest_attributes_event.set()
        return attrs

    async def watch(self) -> AsyncIterator[Status]:
        """Yield status updates from method-6000 pushes, polling as fallback.

        The CC2 pushes partial status deltas (merged into the last full
        snapshot by the message handler). If pushes go quiet for a full
        push period, we poll method 1002 inline instead — the stream
        never terminates on silence, only on ``close()``.
        """
        interval = self.push_period_ms / 1000.0
        queue: asyncio.Queue[Status] = asyncio.Queue(maxsize=64)
        self._status_queues.add(queue)
        try:
            yield await self.status()
            while not self._closed:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=interval)
                except asyncio.TimeoutError:
                    # Push gap — refresh with a full poll and keep going.
                    try:
                        yield await self.status(timeout=5.0)
                    except Exception:
                        log.debug("CC2 watch poll failed", exc_info=True)
        finally:
            self._status_queues.discard(queue)

    async def snapshot(self, *, timeout: float = camera_module.DEFAULT_TIMEOUT) -> bytes:
        return await camera_module.snapshot(
            self.host, timeout=timeout, port=camera_module.CAMERA_PORT_CC2
        )

    # --- control overrides ----------------------------------------------------

    async def start_print(
        self,
        filename: str,
        *,
        storage: str = "local",
        auto_leveling: bool = True,
        timelapse: bool = False,
    ) -> sdcp.ParsedMessage:
        self._require_control("start_print")
        params: dict[str, Any] = {
            "filename": filename,
            "storage_media": storage,
        }
        result = await self._cc2_request(1020, params, timeout=CONTROL_TIMEOUT_S)
        return self._wrap_result(1020, result)

    async def pause(self) -> sdcp.ParsedMessage:
        self._require_control("pause")
        result = await self._cc2_request(1021, {}, timeout=CONTROL_TIMEOUT_S)
        return self._wrap_result(1021, result)

    async def resume(self) -> sdcp.ParsedMessage:
        self._require_control("resume")
        result = await self._cc2_request(1023, {}, timeout=CONTROL_TIMEOUT_S)
        return self._wrap_result(1023, result)

    async def stop(self) -> sdcp.ParsedMessage:
        self._require_control("stop")
        result = await self._cc2_request(1022, {}, timeout=CONTROL_TIMEOUT_S)
        return self._wrap_result(1022, result)

    async def set_print_speed(self, mode: str | int) -> sdcp.ParsedMessage:
        self._require_control("set_print_speed")
        if isinstance(mode, str) and mode.strip().lower() == "auto":
            # Release the pin: stop enforcing and let the printer (and its
            # touchscreen) own the speed mode again. No wire command needed.
            self._pinned_mode = None
            self._pin_candidate = None
            self._mismatch_since = None
            log.info("speed-mode pin released (auto)")
            return self._wrap_result(1031, {"error_code": 0, "pin": "released"})
        if isinstance(mode, str):
            key = mode.strip().lower()
            if key not in self.PRINT_SPEED_MODES:
                raise ValueError(
                    f"unknown print mode {mode!r}; expected one of {sorted(self.PRINT_SPEED_MODES)}"
                )
            value = self.PRINT_SPEED_MODES[key]
        else:
            value = int(mode)
            if value not in self.PRINT_SPEED_MODES.values():
                raise ValueError(
                    f"speed mode {value} not in accepted set "
                    f"{sorted(self.PRINT_SPEED_MODES.values())}"
                )
        result = await self._cc2_request(1031, {"mode": value})
        # An explicit choice through pycentauri pins the mode.
        self._pinned_mode = value
        self._pin_candidate = None
        self._mismatch_since = None
        return self._wrap_result(1031, result)

    async def set_fan_speed(
        self,
        *,
        model: int | None = None,
        auxiliary: int | None = None,
        chamber: int | None = None,
    ) -> sdcp.ParsedMessage:
        self._require_control("set_fan_speed")
        params: dict[str, int] = {}
        for label, key, val in (
            ("model", "fan", model),
            ("auxiliary", "aux_fan", auxiliary),
            ("chamber", "box_fan", chamber),
        ):
            if val is None:
                continue
            if not 0 <= int(val) <= 100:
                raise ValueError(f"fan {label} speed {val} must be 0..100")
            params[key] = int(val)
        if not params:
            raise ValueError("at least one fan speed must be specified")
        result = await self._cc2_request(1030, params)
        return self._wrap_result(1030, result)

    async def set_temperatures(
        self,
        *,
        nozzle: float | None = None,
        bed: float | None = None,
        chamber: float | None = None,
    ) -> sdcp.ParsedMessage:
        self._require_control("set_temperatures")
        params: dict[str, float] = {}
        for label, key, val, lo, hi in (
            ("nozzle", "extruder", nozzle, 0, 300),
            ("bed", "heater_bed", bed, 0, 110),
            ("chamber", "box", chamber, 0, 60),
        ):
            if val is None:
                continue
            if not lo <= float(val) <= hi:
                raise ValueError(f"{label} target {val}°C out of safe range {lo}..{hi}")
            params[key] = float(val)
        if not params:
            raise ValueError("at least one temperature target must be specified")
        result = await self._cc2_request(1028, params)
        return self._wrap_result(1028, result)

    async def canvas_status(self) -> CanvasStatus:
        """Return the Canvas multi-filament system state (method 2005)."""
        result = await self._cc2_request(2005, {})
        return CanvasStatus.from_payload(result)

    async def set_auto_refill(self, enabled: bool) -> sdcp.ParsedMessage:
        """Toggle Canvas auto-refill (method 2004). Requires enable_control."""
        self._require_control("set_auto_refill")
        result = await self._cc2_request(2004, {"auto_refill": enabled})
        return self._wrap_result(2004, result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._ping_task, self._enforce_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._mqtt is not None:
            self._mqtt.disconnect()  # flush DISCONNECT before stopping the loop
            self._mqtt.loop_stop()
        for fut in self._pending_mqtt.values():
            if not fut.done():
                fut.set_exception(PrinterError("connection closed"))

    # --- MQTT internals -------------------------------------------------------

    def _setup_mqtt(self) -> None:
        c = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
            client_id=self._client_id,
            protocol=mqtt.MQTTv311,
        )
        c.username_pw_set(CC2_USERNAME, self.access_code)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        self._mqtt = c

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code == 0 or str(reason_code) == "Success":
            log.debug("MQTT connected to %s", self.host)
            client.subscribe(f"elegoo/{self._serial_number}/#", qos=0)
            # Register on every (re)connect — the printer forgets clients
            # across its own reboots and broker restarts, and without
            # registration it stops routing pushes to us.
            self._publish_register()
            if self._loop:
                self._loop.call_soon_threadsafe(self._mqtt_connected.set)
        else:
            # Surface the reason (e.g. "Not authorized" for a bad access
            # code) instead of letting connect() die with a generic timeout.
            self._connect_error = str(reason_code)
            log.error("MQTT connect failed: %s", reason_code)
            if self._loop:
                self._loop.call_soon_threadsafe(self._mqtt_connected.set)

    def _on_disconnect(self, client: mqtt.Client, *args: Any, **kwargs: Any) -> None:
        if not self._closed:
            log.warning("MQTT disconnected unexpectedly from %s (auto-reconnecting)", self.host)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        """paho callback — runs on the MQTT thread.

        Everything that touches futures, events, queues, or the merged
        status dict is marshaled onto the asyncio loop; this thread only
        parses JSON and routes.
        """
        try:
            data = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if self._loop is None:
            return

        topic = msg.topic
        method = data.get("method")

        if topic == f"elegoo/{self._serial_number}/{self._request_id_prefix}/register_response":
            self._loop.call_soon_threadsafe(self._registered.set)
            return

        # Status pushes (partial deltas) arrive on our api_response topic
        # with method 6000/6008 and their own id sequence — handle them
        # before response correlation so a push can never be mistaken for
        # a command response.
        if method in (6000, 6008):
            if "result" in data:
                self._loop.call_soon_threadsafe(self._apply_push, data["result"])
            return

        # Command responses: only from OUR response topic. The wildcard
        # subscription also delivers other clients' traffic (the broker
        # doesn't isolate clients), and their ids would collide with ours.
        if topic == f"elegoo/{self._serial_number}/{self._client_id}/api_response":
            req_id = data.get("id")
            if isinstance(req_id, int):
                self._loop.call_soon_threadsafe(
                    self._resolve_request, req_id, data.get("result", {})
                )

    def _resolve_request(self, req_id: int, result: dict[str, Any]) -> None:
        """Complete a pending request future. Runs on the asyncio loop."""
        fut = self._pending_mqtt.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _apply_push(self, result: dict[str, Any]) -> None:
        """Merge a partial status delta and fan out. Runs on the asyncio loop.

        Deltas can't stand alone — until the first full method-1002 poll
        has populated ``_last_full_result``, pushes are dropped.
        """
        try:
            if self._last_full_result is None:
                return
            _deep_merge(self._last_full_result, result)
            payload = _cc2_status_to_cc1(self._last_full_result)
            self._track_speed_mode(payload)
            st = Status.from_payload(payload)
            self._latest_status = st
            self._latest_status_event.set()
            for q in list(self._status_queues):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(st)
        except Exception:
            log.exception("failed to handle CC2 status push")

    def _track_speed_mode(self, payload: dict[str, Any]) -> None:
        """Pin-and-enforce the user's speed mode (runs on the asyncio loop).

        The firmware resets ``speed_mode`` to balanced around filament
        switches and occasionally mid-print. Whatever mode the user set —
        through pycentauri (pins immediately) or the touchscreen (pins
        after holding ``PIN_LEARN_S``) — is re-applied whenever the
        printer disagrees for ``ENFORCE_AFTER_S`` while printing. Only
        non-balanced modes are learned from the wire, because a flip to
        balanced is indistinguishable from a firmware reset.
        """
        print_status = payload.get("PrintInfo", {}).get("Status")
        speed_mode = payload.get("_cc2", {}).get("speed_mode")
        if not isinstance(speed_mode, int) or print_status is None:
            return
        if print_status in (0, 8, 9, 14):
            # Print over (idle/stopped/completed/error): the pin dies with it.
            self._pinned_mode = None
            self._pin_candidate = None
            self._mismatch_since = None
            return
        if print_status != 13:
            # Switching filament, pausing, etc. — never learn or enforce
            # here, and a mismatch clock from before doesn't carry across.
            self._mismatch_since = None
            return

        now = self._now()

        # Learn a pin from the wire: only non-balanced modes qualify.
        if speed_mode != 1 and speed_mode != self._pinned_mode:
            if self._pin_candidate == speed_mode:
                if now - self._pin_candidate_since >= PIN_LEARN_S:
                    self._pinned_mode = speed_mode
                    self._pin_candidate = None
                    self._mismatch_since = None
            else:
                self._pin_candidate = speed_mode
                self._pin_candidate_since = now
        elif speed_mode == self._pinned_mode:
            self._pin_candidate = None

        # Enforce the pin.
        if self._pinned_mode is None or speed_mode == self._pinned_mode:
            self._mismatch_since = None
            return
        if self._mismatch_since is None:
            self._mismatch_since = now
            return
        if now - self._mismatch_since < ENFORCE_AFTER_S:
            return
        if self._enforce_task is not None and not self._enforce_task.done():
            return
        if now - self._last_enforce < ENFORCE_MIN_INTERVAL_S:
            return
        self._last_enforce = now
        if self.enable_control:
            self._launch_enforce(self._pinned_mode)
        else:
            log.warning(
                "speed_mode drifted to %d but pin is %d; enable_control is off "
                "so it will not be re-applied",
                speed_mode,
                self._pinned_mode,
            )

    def _launch_enforce(self, mode: int) -> None:
        self._enforce_task = asyncio.create_task(
            self._apply_pinned_mode(mode),
            name=f"pycentauri-cc2-speedpin-{self.host}",
        )

    async def _apply_pinned_mode(self, mode: int) -> None:
        try:
            await self._cc2_request(1031, {"mode": mode})
            log.info("re-applied pinned speed_mode %d", mode)
        except Exception:
            log.warning("could not re-apply pinned speed_mode %d", mode, exc_info=True)

    def _publish_register(self) -> None:
        topic = f"elegoo/{self._serial_number}/api_register"
        payload = json.dumps({"client_id": self._client_id, "request_id": self._request_id_prefix})
        assert self._mqtt is not None
        self._mqtt.publish(topic, payload, qos=0)

    async def _cc2_request(
        self,
        method: int,
        params: dict[str, Any],
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        self._req_counter += 1
        req_id = self._req_counter
        assert self._mqtt is not None
        assert self._loop is not None

        fut: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending_mqtt[req_id] = fut

        topic = f"elegoo/{self._serial_number}/{self._client_id}/api_request"
        body = json.dumps({"id": req_id, "method": method, "params": params})
        self._mqtt.publish(topic, body, qos=0)

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as err:
            raise RequestTimeoutError(f"CC2 method {method} timed out after {timeout}s") from err
        finally:
            self._pending_mqtt.pop(req_id, None)

        error_code = result.get("error_code", 0)
        if error_code != 0:
            raise PrinterError(f"CC2 method {method} returned error_code={error_code}")
        return result

    @staticmethod
    def _wrap_result(method: int, result: dict[str, Any]) -> sdcp.ParsedMessage:
        inner = {"Cmd": method, "Data": {"Ack": 0}, **result}
        return sdcp.ParsedMessage(
            type=sdcp.MessageType.RESPONSE,
            raw=inner,
            inner=inner,
            mainboard_id=None,
            request_id=None,
            status=None,
            attributes=None,
        )


async def _fetch_serial(host: str, access_code: str, timeout: float = 5.0) -> str:
    """Fetch the serial number from the CC2's HTTP API."""
    import httpx

    url = f"http://{host}/system/info"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params={"X-Token": access_code})
        if resp.status_code == 401:
            raise PrinterError(
                f"{host} rejected the access code (HTTP 401). "
                "Check the code shown on the printer's screen."
            )
        resp.raise_for_status()
        data = resp.json()
    sn: str = data.get("system_info", {}).get("sn", "")
    if not sn:
        raise PrinterError(f"failed to fetch serial number from {host}: {data}")
    return sn
