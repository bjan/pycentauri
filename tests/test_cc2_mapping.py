"""Unit tests for the CC2 → CC1 translation layer (pure functions).

These lock down what every surface reports about a CC2's state: the
machine_status/sub_status → PrintInfo.Status mapping (including the
position-based filament-switch detection), the partial-delta deep merge,
the full status payload translation, and Canvas payload parsing. No MQTT,
no printer, no fakes.
"""

from __future__ import annotations

from typing import Any

import pytest

from pycentauri.cc2 import (
    _cc2_attrs_to_cc1,
    _cc2_machine_status_to_print_status,
    _cc2_status_to_cc1,
    _deep_merge,
)
from pycentauri.models import CanvasStatus, Status

# --- state mapping -----------------------------------------------------------


def test_idle_and_initializing_map_to_idle() -> None:
    assert _cc2_machine_status_to_print_status(0, 0) == 0
    assert _cc2_machine_status_to_print_status(1, 0) == 0


def test_plain_printing() -> None:
    assert _cc2_machine_status_to_print_status(2, 2075, head_y=120.0, progress=19) == 13


def test_explicit_substates_win() -> None:
    assert _cc2_machine_status_to_print_status(2, 2501) == 5  # pausing
    assert _cc2_machine_status_to_print_status(2, 2502) == 6  # paused
    assert _cc2_machine_status_to_print_status(2, 2505) == 6  # paused (alt)
    assert _cc2_machine_status_to_print_status(2, 2401) == 12  # resuming
    assert _cc2_machine_status_to_print_status(2, 2503) == 7  # stopping
    assert _cc2_machine_status_to_print_status(2, 2504) == 8  # stopped
    assert _cc2_machine_status_to_print_status(2, 2077) == 9  # completed
    assert _cc2_machine_status_to_print_status(2, 2801) == 1  # homing
    assert _cc2_machine_status_to_print_status(2, 2901) == 15  # leveling


def test_filament_switch_detected_by_purge_zone_park() -> None:
    # Captured live 2026-07-04: mid-print Canvas switch keeps
    # machine_status=2 / sub_status=2075 with the head parked at y=264.
    assert _cc2_machine_status_to_print_status(2, 2075, head_y=264.0, progress=19) == 27
    # The sub-second preheat blips during the switch also read as 27
    # because position wins over the preheat sub_status.
    assert _cc2_machine_status_to_print_status(2, 1045, head_y=264.0, progress=19) == 27


def test_end_of_print_park_is_not_a_filament_switch() -> None:
    # Same park position, but progress=100 → the progress guard holds.
    assert _cc2_machine_status_to_print_status(2, 2075, head_y=264.0, progress=100) == 13
    # Explicit completion sub_status wins outright.
    assert _cc2_machine_status_to_print_status(2, 2077, head_y=264.0, progress=100) == 9


def test_paused_at_park_reads_paused_not_switching() -> None:
    assert _cc2_machine_status_to_print_status(2, 2502, head_y=264.0, progress=50) == 6


def test_preheating_on_the_bed_is_preheating() -> None:
    assert _cc2_machine_status_to_print_status(2, 1045, head_y=120.0) == 16
    assert _cc2_machine_status_to_print_status(2, 1405, head_y=120.0) == 16


def test_manual_filament_operating_states() -> None:
    assert _cc2_machine_status_to_print_status(3, 1133) == 27
    assert _cc2_machine_status_to_print_status(4, 1136) == 28
    assert _cc2_machine_status_to_print_status(4, 1144) == 29
    assert _cc2_machine_status_to_print_status(4, 0) == 27


def test_unknown_status_does_not_collide_with_cc1_codes() -> None:
    # A hypothetical future machine_status must not leak through as a
    # raw value that happens to mean something else in the CC1 space.
    assert _cc2_machine_status_to_print_status(7, 0) == 99


# --- deep merge --------------------------------------------------------------


def test_deep_merge_updates_nested_without_clobbering_siblings() -> None:
    base: dict[str, Any] = {
        "extruder": {"temperature": 200, "target": 220},
        "fans": {"fan": {"speed": 100.0}, "aux_fan": {"speed": 50.0}},
    }
    _deep_merge(base, {"extruder": {"temperature": 205}})
    assert base["extruder"] == {"temperature": 205, "target": 220}
    assert base["fans"]["aux_fan"]["speed"] == 50.0

    _deep_merge(base, {"fans": {"fan": {"speed": 0.0}}})
    assert base["fans"]["fan"]["speed"] == 0.0
    assert base["fans"]["aux_fan"]["speed"] == 50.0


def test_deep_merge_replaces_non_dict_values() -> None:
    base: dict[str, Any] = {"machine_status": {"exception_status": [1]}}
    _deep_merge(base, {"machine_status": {"exception_status": []}})
    assert base["machine_status"]["exception_status"] == []


# --- full status translation --------------------------------------------------


def _full_cc2_result(**overrides: Any) -> dict[str, Any]:
    """A representative method-1002 result (from live capture)."""
    result: dict[str, Any] = {
        "error_code": 0,
        "extruder": {
            "filament_detect_enable": 1,
            "filament_detected": 1,
            "target": 220,
            "temperature": 219.5,
        },
        "fans": {
            "aux_fan": {"speed": 0.0},
            "box_fan": {"speed": 25.5},
            "controller_fan": {"speed": 255.0},
            "fan": {"speed": 100.0},
            "heater_fan": {"speed": 255.0},
        },
        "gcode_move": {
            "extruder": 10.0,
            "speed": 3000,
            "speed_mode": 1,
            "x": 104.1,
            "y": 170.8,
            "z": 9.0,
        },
        "heater_bed": {"target": 60, "temperature": 59.8},
        "led": {"status": 1},
        "machine_status": {
            "exception_status": [],
            "progress": 19,
            "status": 2,
            "sub_status": 2075,
            "sub_status_reason_code": 0,
        },
        "print_status": {
            "current_layer": 43,
            "filename": "duck.gcode",
            "print_duration": 10427,
            "remaining_time_sec": 40683,
            "state": "printing",
            "total_duration": 10599,
            "uuid": "abc-123",
        },
        "ztemperature_sensor": {"temperature": 30},
        "external_device": {"camera": True, "type": "0303", "u_disk": True},
        "tool_head": {"homed_axes": "xyz"},
    }
    _deep_merge(result, overrides)
    return result


def test_status_translation_round_trip() -> None:
    st = Status.from_payload(_cc2_status_to_cc1(_full_cc2_result()))
    assert st.print_status == 13
    assert st.progress == 19
    assert st.filename == "duck.gcode"
    assert st.temp_nozzle == 219.5
    assert st.temp_nozzle_target == 220
    assert st.temp_bed == 59.8
    assert st.coord == (104.1, 170.8, 9.0)
    assert st.fan_speed["ModelFan"] == 100
    assert st.fan_speed["ControllerFan"] == 255
    assert st.raw["_cc2"]["gcode_move_speed"] == 3000
    assert st.raw["_cc2"]["speed_mode"] == 1
    assert st.raw["_cc2"]["remaining_time_sec"] == 40683
    # TotalTicks = elapsed + remaining, so the UI's ETA math works unchanged
    pi = st.print_info
    assert pi is not None
    assert pi.total_ticks == 10427 + 40683


def test_status_translation_speed_mode_to_pct() -> None:
    st = Status.from_payload(_cc2_status_to_cc1(_full_cc2_result(gcode_move={"speed_mode": 3})))
    assert st.print_info is not None
    assert st.print_info.print_speed == 160


def test_status_translation_switch_in_progress() -> None:
    st = Status.from_payload(
        _cc2_status_to_cc1(_full_cc2_result(gcode_move={"x": 52.5, "y": 264.0}))
    )
    assert st.print_status == 27


def test_attrs_translation() -> None:
    attrs_payload = _cc2_attrs_to_cc1(
        {
            "hostname": "Centauri Carbon 2",
            "machine_model": "Centauri Carbon 2",
            "sn": "F01TEST",
            "protocol_version": "1.0.0",
            "software_version": {"ota_version": "01.03.02.51"},
        }
    )
    assert attrs_payload["MainboardID"] == "F01TEST"
    assert attrs_payload["FirmwareVersion"] == "01.03.02.51"


# --- canvas parsing ------------------------------------------------------------


CANVAS_RESULT = {
    "canvas_info": {
        "active_canvas_id": 0,
        "active_tray_id": -1,
        "auto_refill": False,
        "canvas_list": [
            {
                "canvas_id": 0,
                "connected": 1,
                "tray_list": [
                    {
                        "brand": "Generic",
                        "filament_code": "0x0008",
                        "filament_color": "#F72221",
                        "filament_name": "PLA Wood",
                        "filament_type": "PLA",
                        "max_nozzle_temp": 230,
                        "min_nozzle_temp": 190,
                        "status": 1,
                        "tray_id": 0,
                    },
                    {
                        "brand": "Generic",
                        "filament_code": "0x0100",
                        "filament_color": "#A03BF7",
                        "filament_name": "PETG",
                        "filament_type": "PETG",
                        "max_nozzle_temp": 260,
                        "min_nozzle_temp": 230,
                        "status": 0,
                        "tray_id": 2,
                    },
                ],
            }
        ],
    },
    "error_code": 0,
}


def test_canvas_parse() -> None:
    cs = CanvasStatus.from_payload(CANVAS_RESULT)
    assert cs.connected is True
    assert cs.auto_refill is False
    assert cs.active_tray_id == -1
    assert cs.tray_count == 2
    tray = cs.canvas_list[0].tray_list[0]
    assert tray.filament_name == "PLA Wood"
    assert tray.filament_color == "#F72221"
    assert tray.status == 1


def test_canvas_parse_survives_malformed_payloads() -> None:
    assert CanvasStatus.from_payload({}).connected is False
    assert CanvasStatus.from_payload({"canvas_info": None}).tray_count == 0
    assert (
        CanvasStatus.from_payload(
            {"canvas_info": {"canvas_list": [{"tray_list": "bogus"}]}}
        ).tray_count
        == 0
    )
    assert CanvasStatus.from_payload({"canvas_info": {"canvas_list": "bogus"}}).tray_count == 0


# --- speed-mode pin (explicit; re-applied on switch completion) -----------
#
# Firmware 02.01.00.00 streams gcode_move.speed_mode as a per-move value, so
# pycentauri no longer infers or enforces the pin from the wire. The pin is
# explicit, cleared at print end, and re-asserted once when a Canvas filament
# switch completes (27 -> 13).


def _payload(status: int, mode: int = 1) -> dict[str, Any]:
    return {"PrintInfo": {"Status": status}, "_cc2": {"speed_mode": mode}}


def _printer(enable_control: bool = True) -> Any:
    from pycentauri.cc2 import CC2Printer

    p = CC2Printer("127.0.0.1", access_code="x", enable_control=enable_control)
    p._fired: list[int] = []
    # Record re-apply calls instead of spawning a real 1031 task.
    p._reapply_pin = lambda: p._fired.append(p._pinned_mode)
    t = [0.0]
    p._clock = t
    p._now = lambda: t[0]
    return p


def _tick(p: Any, dt: float, status: int, mode: int = 1) -> None:
    p._clock[0] += dt
    p._track_speed_mode(_payload(status, mode))


def test_pin_reapplied_once_on_switch_completion() -> None:
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13)  # printing
    _tick(p, 5, 27)  # filament switch (parked)
    _tick(p, 180, 27)  # switch runs
    assert p._fired == []  # nothing re-applied mid-switch
    _tick(p, 1, 13)  # switch complete -> re-assert once
    assert p._fired == [2]
    _tick(p, 2, 13)  # keeps printing, no repeat
    assert p._fired == [2]


def test_wire_jitter_never_mutates_the_pin() -> None:
    """Per-move speed_mode oscillation must not change or release the pin."""
    p = _printer()
    p._pinned_mode = 2  # user set sport
    # Oscillating wire values across a long printing stretch, no switch.
    for i in range(40):
        _tick(p, 1.0, 13, mode=(1 if i % 2 == 0 else 2))
    assert p._pinned_mode == 2  # not released, not re-learned
    assert p._fired == []  # no unsolicited re-apply


def test_sustained_balanced_without_switch_keeps_pin() -> None:
    """A long balanced-speed feature (>12s) must NOT release the pin — this
    was the bug where the old logic mistook it for a human choosing balanced."""
    p = _printer()
    p._pinned_mode = 2
    for _ in range(30):
        _tick(p, 1.0, 13, mode=1)  # 30s of wire=balanced, no switch
    assert p._pinned_mode == 2
    assert p._fired == []


def test_no_pin_no_reapply_on_switch() -> None:
    p = _printer()
    _tick(p, 0, 13)
    _tick(p, 5, 27)
    _tick(p, 60, 13)  # switch completes but nothing is pinned
    assert p._fired == []
    assert p._pinned_mode is None


def test_print_end_clears_pin() -> None:
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13)
    _tick(p, 5, 9)  # completed
    assert p._pinned_mode is None
    _tick(p, 5, 13)  # a fresh print
    _tick(p, 5, 27)
    _tick(p, 5, 13)  # switch completes but pin was cleared
    assert p._fired == []


def test_reapply_only_on_27_to_13_not_other_transitions() -> None:
    p = _printer()
    p._pinned_mode = 3
    _tick(p, 0, 13)
    _tick(p, 5, 6)  # paused
    _tick(p, 5, 13)  # resume from pause (not a switch) -> no re-apply
    assert p._fired == []


# --- HTTP bootstrap error surfacing -------------------------------------------


async def test_fetch_serial_connect_error_names_lan_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused HTTP :80 bootstrap must raise a PrinterError that points at
    the CC2's 'LAN Only' setting, not leak a raw httpx ConnectError."""
    import httpx

    from pycentauri import cc2
    from pycentauri.client import PrinterError

    class _FailClient:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _FailClient:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        async def get(self, *a: Any, **k: Any) -> Any:
            raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(httpx, "AsyncClient", _FailClient)
    with pytest.raises(PrinterError, match="LAN Only"):
        await cc2._fetch_serial("192.0.2.1", "code")


# --- speed-mode display stabilization (firmware 02.01.00.00 per-move jitter) ---


def _sm_payload(speed_mode: int) -> dict[str, Any]:
    return {"PrintInfo": {"Status": 13}, "_cc2": {"speed_mode": speed_mode}}


def test_pin_drives_stable_display_despite_wire_jitter() -> None:
    p = _printer()
    p._pinned_mode = 2  # user set sport via pycentauri
    # The wire oscillates 1/2 every push (per-move value); display must hold sport.
    for raw in (1, 2, 1, 2, 1, 2):
        payload = _sm_payload(raw)
        p._stabilize_speed_mode(payload)
        assert payload["_cc2"]["speed_mode"] == 2
        assert payload["PrintInfo"]["PrintSpeedPct"] == 130


def test_debounce_holds_display_when_no_pin() -> None:
    from pycentauri.cc2 import SPEED_DISPLAY_DEBOUNCE_S

    p = _printer()
    p._pinned_mode = None
    t = [0.0]
    p._now = lambda: t[0]
    # Bootstraps to the first value seen.
    payload = _sm_payload(2)
    p._stabilize_speed_mode(payload)
    assert payload["_cc2"]["speed_mode"] == 2
    # Fast oscillation never persists the debounce window → display stays at 2.
    for i in range(8):
        t[0] += 1.0
        payload = _sm_payload(1 if i % 2 == 0 else 2)
        p._stabilize_speed_mode(payload)
        assert payload["_cc2"]["speed_mode"] == 2
    # A value that DOES persist past the window is adopted.
    for _ in range(int(SPEED_DISPLAY_DEBOUNCE_S) + 2):
        t[0] += 1.0
        payload = _sm_payload(3)
        p._stabilize_speed_mode(payload)
    assert payload["_cc2"]["speed_mode"] == 3
