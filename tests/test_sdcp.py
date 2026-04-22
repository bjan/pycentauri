"""Unit tests for the SDCP envelope and parser. No network required."""

from __future__ import annotations

import json

from pycentauri.sdcp import (
    Cmd,
    MessageType,
    build_request,
    build_subscribe,
    encode,
    parse_message,
)

MAINBOARD = "ffffffff"


def test_build_request_shape():
    pkt = build_request(Cmd.GET_PRINTER_STATUS, None, MAINBOARD)
    assert set(pkt) == {"Id", "Data", "Topic"}
    assert pkt["Topic"] == f"sdcp/request/{MAINBOARD}"
    data = pkt["Data"]
    assert data["Cmd"] == int(Cmd.GET_PRINTER_STATUS)
    assert data["MainboardID"] == MAINBOARD
    assert data["From"] == 1
    assert data["Data"] == {}
    assert isinstance(data["RequestID"], str) and data["RequestID"]
    assert isinstance(data["TimeStamp"], int)


def test_build_request_uses_passed_ids():
    pkt = build_request(
        Cmd.START_PRINT,
        {"Filename": "cube.gcode"},
        MAINBOARD,
        request_id="deadbeef",
        envelope_id="abc123",
    )
    assert pkt["Id"] == "abc123"
    assert pkt["Data"]["RequestID"] == "deadbeef"
    assert pkt["Data"]["Data"] == {"Filename": "cube.gcode"}


def test_build_request_rejects_empty_mainboard():
    try:
        build_request(Cmd.GET_PRINTER_STATUS, None, "")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_build_subscribe():
    pkt = build_subscribe(MAINBOARD, period_ms=2500)
    assert pkt["Data"]["Cmd"] == int(Cmd.SUBSCRIBE)
    assert pkt["Data"]["Data"] == {"TimePeriod": 2500}


def test_encode_is_compact_json():
    pkt = build_subscribe(MAINBOARD)
    s = encode(pkt)
    assert s == json.dumps(pkt, separators=(",", ":"))
    assert " " not in s[:50]  # compact separators


def test_parse_status_push():
    payload = {
        "Id": MAINBOARD,
        "Topic": f"sdcp/status/{MAINBOARD}",
        "Data": {
            "MainboardID": MAINBOARD,
            "Status": {
                "CurrenStatus": [13],
                "TempOfNozzle": [210.0, 208.7],
                "TempOfHotbed": [60.0, 59.3],
                "TempOfBox": [40.0, 38.1],
                "PrintInfo": {
                    "Status": 13,
                    "Filename": "cube.gcode",
                    "CurrentLayer": 42,
                    "TotalLayer": 100,
                    "Progress": 42,
                },
            },
        },
    }
    msg = parse_message(payload)
    assert msg.type == MessageType.STATUS
    assert msg.status is not None
    assert msg.status["PrintInfo"]["Filename"] == "cube.gcode"
    assert msg.mainboard_id == MAINBOARD


def test_parse_attributes_push():
    payload = {
        "Id": MAINBOARD,
        "Topic": f"sdcp/attributes/{MAINBOARD}",
        "Data": {
            "MainboardID": MAINBOARD,
            "Attributes": {
                "MainboardID": MAINBOARD,
                "Name": "Centauri Carbon",
                "MachineName": "Centauri Carbon",
                "FirmwareVersion": "V1.0.0",
            },
        },
    }
    msg = parse_message(payload)
    assert msg.type == MessageType.ATTRIBUTES
    assert msg.attributes is not None
    assert msg.attributes["FirmwareVersion"] == "V1.0.0"
    assert msg.mainboard_id == MAINBOARD


def test_parse_response_envelope():
    payload = {
        "Id": MAINBOARD,
        "Topic": f"sdcp/response/{MAINBOARD}",
        "Data": {
            "Cmd": 512,
            "RequestID": "abcdef01",
            "MainboardID": MAINBOARD,
            "Data": {"Ack": 0},
        },
    }
    msg = parse_message(payload)
    assert msg.type == MessageType.RESPONSE
    assert msg.request_id == "abcdef01"
    assert msg.inner is not None and msg.inner["Data"] == {"Ack": 0}


def test_parse_handles_length_prefix():
    # Some SDCP frames arrive as "NNN{json...}" — we should strip digits.
    payload = {
        "Id": MAINBOARD,
        "Topic": f"sdcp/status/{MAINBOARD}",
        "Data": {"MainboardID": MAINBOARD, "Status": {"CurrenStatus": [1]}},
    }
    raw = "123" + json.dumps(payload)
    msg = parse_message(raw)
    assert msg.type == MessageType.STATUS


def test_parse_unknown_on_garbage():
    msg = parse_message("not json")
    assert msg.type == MessageType.UNKNOWN


def test_parse_accepts_bytes():
    pkt = build_subscribe(MAINBOARD)
    s = encode(pkt).encode("utf-8")
    msg = parse_message(s)
    # Outgoing request echoed — classified UNKNOWN (no status/attributes topic).
    assert msg.type == MessageType.UNKNOWN
    assert msg.raw["Data"]["Cmd"] == int(Cmd.SUBSCRIBE)
