"""Unit tests for the CC2 → CC1 translation layer (pure functions).

These lock down what every surface reports about a CC2's state: the
machine_status/sub_status → PrintInfo.Status mapping (including the
position-based filament-switch detection), the partial-delta deep merge,
the full status payload translation, and Canvas payload parsing. No MQTT,
no printer, no fakes.
"""

from __future__ import annotations

from typing import Any

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


# --- speed-mode pin & enforce ---------------------------------------------


def _payload(status: int, mode: int) -> dict[str, Any]:
    return {"PrintInfo": {"Status": status}, "_cc2": {"speed_mode": mode}}


def _printer(enable_control: bool = True) -> Any:
    from pycentauri.cc2 import CC2Printer

    p = CC2Printer("127.0.0.1", access_code="x", enable_control=enable_control)
    p._fired: list[int] = []
    p._launch_enforce = p._fired.append  # record instead of spawning tasks
    t = [0.0]
    p._clock = t
    p._now = lambda: t[0]
    return p


def _tick(p: Any, dt: float, status: int, mode: int) -> None:
    p._clock[0] += dt
    p._track_speed_mode(_payload(status, mode))


def test_api_set_pins_immediately_and_enforces_after_grace() -> None:
    p = _printer()
    p._pinned_mode = 2  # what set_print_speed() records
    _tick(p, 0, 13, 2)
    assert p._fired == []
    # Firmware resets to balanced mid-print; not enforced until it holds 12 s
    _tick(p, 5, 13, 1)
    _tick(p, 5, 13, 1)
    assert p._fired == []
    _tick(p, 5, 13, 1)  # mismatch has now persisted 10 s... (first at t=10)
    _tick(p, 5, 13, 1)  # 15 s since mismatch start -> fires
    assert p._fired == [2]


def test_pre_switch_reset_window_never_fires() -> None:
    """The 6-10 s balanced window before the head parks stays untouched."""
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13, 2)
    _tick(p, 3, 13, 1)  # pre-switch reset
    _tick(p, 3, 13, 1)  # 6 s in
    _tick(p, 3, 27, 1)  # head parks -> mismatch clock cleared
    _tick(p, 60, 27, 1)  # switch runs
    assert p._fired == []
    # Resume: reset persists, enforcement kicks in after the grace period
    _tick(p, 5, 13, 1)
    _tick(p, 13, 13, 1)
    assert p._fired == [2]


def test_screen_change_to_nonbalanced_becomes_pin() -> None:
    p = _printer()
    _tick(p, 0, 13, 1)  # printing at balanced, nothing pinned
    _tick(p, 5, 13, 3)  # user picks ludicrous on the touchscreen
    assert p._pinned_mode is None
    _tick(p, 16, 13, 3)  # held > PIN_LEARN_S -> adopted
    assert p._pinned_mode == 3
    assert p._fired == []


def test_balanced_is_never_learned_as_pin_while_pinned() -> None:
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13, 2)
    # Sustained balanced (reset or screen tap) never displaces the pin...
    _tick(p, 10, 13, 1)
    _tick(p, 30, 13, 1)
    assert p._pinned_mode == 2
    # ...and enforcement fired for it
    assert p._fired == [2]


def test_no_pin_means_no_enforcement() -> None:
    p = _printer()
    _tick(p, 0, 13, 1)
    _tick(p, 60, 13, 1)
    assert p._fired == []


def test_print_end_clears_pin() -> None:
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13, 2)
    _tick(p, 5, 9, 1)  # completed
    assert p._pinned_mode is None
    _tick(p, 5, 13, 1)  # a new print at balanced: nothing enforced
    _tick(p, 60, 13, 1)
    assert p._fired == []


def test_enforce_rate_limited() -> None:
    p = _printer()
    p._pinned_mode = 2
    _tick(p, 0, 13, 2)
    _tick(p, 5, 13, 1)
    _tick(p, 13, 13, 1)  # fires (mismatch held 13 s)
    _tick(p, 5, 13, 1)  # still mismatched, but inside the 30 s cooldown
    _tick(p, 5, 13, 1)
    assert p._fired == [2]
    _tick(p, 30, 13, 1)  # cooldown over -> fires again
    assert p._fired == [2, 2]


def test_screen_override_beats_enforcement() -> None:
    """A touchscreen change to a non-balanced mode must be learned BEFORE
    enforcement of the old pin would revert it (PIN_LEARN_S < ENFORCE_AFTER_S)."""
    p = _printer()
    p._pinned_mode = 1  # balanced pinned via the API
    _tick(p, 0, 13, 1)
    _tick(p, 2, 13, 2)  # user picks sport on the screen (mismatch clock starts)
    _tick(p, 5, 13, 2)  # 5 s held
    _tick(p, 4, 13, 2)  # 9 s held: learned as the new pin, before 12 s enforcement
    assert p._pinned_mode == 2
    assert p._fired == []


def test_auto_releases_pin() -> None:
    import asyncio

    p = _printer()
    p._pinned_mode = 2

    async def run() -> None:
        result = await p.set_print_speed("auto")
        assert result.inner["pin"] == "released"

    asyncio.run(run())
    assert p._pinned_mode is None
    _tick(p, 0, 13, 1)
    _tick(p, 60, 13, 1)  # balanced persists, nothing enforced
    assert p._fired == []
