"""Unit tests for discovery response parsing.

We test the parser against canned responses rather than exercising the full
UDP round-trip — loopback UDP timing is flaky across CI runners (macOS and
some Python versions drop packets sent to 127.0.0.1 from a wildcard-bound
socket within a short window). The actual broadcast is verified live against
a real printer.
"""

from __future__ import annotations

import json

from pycentauri.discovery import _parse_response


def test_parse_response_populates_fields() -> None:
    raw = json.dumps(
        {
            "Id": "fake",
            "Data": {
                "Name": "fake-carbon",
                "MachineName": "Centauri Carbon",
                "MainboardID": "ffffffff",
                "FirmwareVersion": "V0.0.1",
            },
        }
    ).encode("utf-8")

    p = _parse_response(raw, "192.168.1.209")
    assert p is not None
    assert p.host == "192.168.1.209"
    assert p.mainboard_id == "ffffffff"
    assert p.name == "fake-carbon"
    assert p.machine_name == "Centauri Carbon"
    assert p.firmware_version == "V0.0.1"


def test_parse_response_handles_missing_keys() -> None:
    """Older firmware may omit some fields — parser should still return a row."""
    raw = json.dumps({"Id": "fake", "Data": {"MainboardID": "aa"}}).encode("utf-8")
    p = _parse_response(raw, "10.0.0.5")
    assert p is not None
    assert p.mainboard_id == "aa"
    assert p.name is None
    assert p.machine_name is None


def test_parse_response_rejects_non_json() -> None:
    assert _parse_response(b"M99999", "10.0.0.5") is None
    assert _parse_response(b"\xff\xfe", "10.0.0.5") is None


def test_parse_response_rejects_non_object() -> None:
    assert _parse_response(b'"just a string"', "10.0.0.5") is None
    assert _parse_response(b"[1, 2, 3]", "10.0.0.5") is None


def test_parse_response_tolerates_missing_data_block() -> None:
    raw = json.dumps({"Id": "fake", "MainboardID": "bb"}).encode("utf-8")
    p = _parse_response(raw, "10.0.0.5")
    assert p is not None
    assert p.mainboard_id == "bb"
