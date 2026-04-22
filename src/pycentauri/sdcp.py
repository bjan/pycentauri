"""SDCP v3 wire protocol for the Elegoo Centauri Carbon.

The Centauri Carbon exposes a WebSocket at ``ws://<host>:3030/websocket`` that
speaks Elegoo's Smart Device Control Protocol (SDCP). Messages are JSON; the
envelope wraps a command and routing topic.

Reference implementations consulted:

* ``ELEGOO-3D/elegoo-link`` — official C++ SDK, ``elegoo_fdm_cc_message_adapter.cpp``
* ``CentauriLink/Centauri-Link`` — community Python/Kivy client, ``main.py``
"""

from __future__ import annotations

import json
import secrets
import time
import uuid
from enum import IntEnum
from typing import Any


class Cmd(IntEnum):
    """SDCP command codes for the original Centauri Carbon.

    Codes confirmed against the official elegoo-link C++ SDK's
    ``COMMAND_MAPPING_TABLE`` in ``elegoo_fdm_cc_message_adapter.cpp``. Cmd 512
    is documented by CentauriLink and OctoEverywhere as the status-push
    subscribe command.
    """

    GET_PRINTER_STATUS = 0
    GET_PRINTER_ATTRIBUTES = 1
    START_PRINT = 128
    PAUSE_PRINT = 129
    STOP_PRINT = 130
    RESUME_PRINT = 131
    GET_CANVAS_STATUS = 324
    SUBSCRIBE = 512


DEFAULT_PUSH_PERIOD_MS = 5000


class MessageType(IntEnum):
    """Parsed message categories, based on the ``Topic`` field."""

    UNKNOWN = 0
    RESPONSE = 1
    STATUS = 2
    ATTRIBUTES = 3
    NOTICE = 4


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_request_id() -> str:
    return secrets.token_hex(8)


def _new_envelope_id() -> str:
    return uuid.uuid4().hex


def build_request(
    cmd: int,
    data: dict[str, Any] | None,
    mainboard_id: str,
    *,
    request_id: str | None = None,
    envelope_id: str | None = None,
) -> dict[str, Any]:
    """Build a fully-formed SDCP request packet.

    The packet structure mirrors what ``ElegooFdmCCMessageAdapter`` emits:

    .. code-block:: json

        {
          "Id": "<mainboard id or uuid>",
          "Data": {
            "Cmd": 0,
            "Data": {},
            "RequestID": "<hex>",
            "MainboardID": "<serial>",
            "TimeStamp": 1687069655000,
            "From": 1
          },
          "Topic": "sdcp/request/<mainboard id>"
        }

    ``MainboardID`` is mandatory for every outbound command; obtain it from a
    discovery response or from the first ``Attributes`` push the printer sends
    after the WebSocket connects.
    """
    if not mainboard_id:
        raise ValueError("mainboard_id is required for SDCP commands")
    return {
        "Id": envelope_id or mainboard_id,
        "Data": {
            "Cmd": int(cmd),
            "Data": data or {},
            "RequestID": request_id or _new_request_id(),
            "MainboardID": mainboard_id,
            "TimeStamp": _now_ms(),
            "From": 1,
        },
        "Topic": f"sdcp/request/{mainboard_id}",
    }


def build_subscribe(mainboard_id: str, period_ms: int = DEFAULT_PUSH_PERIOD_MS) -> dict[str, Any]:
    """Cmd 512 — request status pushes every ``period_ms`` milliseconds."""
    return build_request(Cmd.SUBSCRIBE, {"TimePeriod": int(period_ms)}, mainboard_id)


def encode(packet: dict[str, Any]) -> str:
    """Serialize an envelope to JSON for transmission over the WebSocket."""
    return json.dumps(packet, separators=(",", ":"))


def _classify(topic: str, payload: dict[str, Any]) -> MessageType:
    if "sdcp/status" in topic or "Status" in payload:
        return MessageType.STATUS
    if "sdcp/attributes" in topic or "Attributes" in payload:
        return MessageType.ATTRIBUTES
    if "sdcp/response" in topic:
        return MessageType.RESPONSE
    if "sdcp/notice" in topic:
        return MessageType.NOTICE
    return MessageType.UNKNOWN


class ParsedMessage:
    """Result of :func:`parse_message`, flattening the nested envelope.

    ``raw`` is the top-level dict; ``inner`` is ``raw["Data"]`` when present
    (command responses put the useful payload there). ``status`` and
    ``attributes`` are non-None for the matching topics. ``request_id`` links
    the message back to the request that triggered it.
    """

    __slots__ = ("attributes", "inner", "mainboard_id", "raw", "request_id", "status", "type")

    def __init__(
        self,
        *,
        type: MessageType,
        raw: dict[str, Any],
        inner: dict[str, Any] | None = None,
        status: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
        request_id: str | None = None,
        mainboard_id: str | None = None,
    ) -> None:
        self.type = type
        self.raw = raw
        self.inner = inner
        self.status = status
        self.attributes = attributes
        self.request_id = request_id
        self.mainboard_id = mainboard_id

    def __repr__(self) -> str:
        return (
            f"ParsedMessage(type={self.type.name}, request_id={self.request_id!r}, "
            f"mainboard_id={self.mainboard_id!r})"
        )


def parse_message(raw: str | bytes | dict[str, Any]) -> ParsedMessage:
    """Parse an incoming SDCP message from the printer.

    Accepts either a raw text/bytes frame or an already-parsed dict.
    Unrecognised or malformed payloads come back as
    ``ParsedMessage(type=UNKNOWN)`` with ``raw`` populated so callers can log.
    """
    if isinstance(raw, dict):
        obj = raw
    else:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        # Some SDCP variants prefix messages with a decimal length.
        text = text.lstrip()
        if text and text[0].isdigit():
            first_brace = text.find("{")
            if first_brace > 0:
                text = text[first_brace:]
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return ParsedMessage(type=MessageType.UNKNOWN, raw={"_raw": raw})
        if not isinstance(obj, dict):
            return ParsedMessage(type=MessageType.UNKNOWN, raw={"_raw": obj})

    topic = obj.get("Topic") or ""
    msg_type = _classify(topic if isinstance(topic, str) else "", obj)

    data = obj.get("Data") if isinstance(obj.get("Data"), dict) else None
    mainboard_id = obj.get("MainboardID")
    if mainboard_id is None and data is not None:
        mainboard_id = data.get("MainboardID")
    request_id = None
    if data is not None:
        request_id = data.get("RequestID")

    status_payload: dict[str, Any] | None = None
    attributes_payload: dict[str, Any] | None = None

    if msg_type == MessageType.STATUS:
        if isinstance(obj.get("Status"), dict):
            status_payload = obj["Status"]
        elif data is not None and isinstance(data.get("Status"), dict):
            status_payload = data["Status"]
        elif data is not None:
            status_payload = {k: v for k, v in data.items() if k != "MainboardID"}

    if msg_type == MessageType.ATTRIBUTES:
        if isinstance(obj.get("Attributes"), dict):
            attributes_payload = obj["Attributes"]
        elif data is not None and isinstance(data.get("Attributes"), dict):
            attributes_payload = data["Attributes"]
        elif data is not None:
            attributes_payload = {k: v for k, v in data.items() if k != "MainboardID"}
        # Attributes carries MainboardID too.
        if mainboard_id is None and attributes_payload is not None:
            mainboard_id = attributes_payload.get("MainboardID")

    return ParsedMessage(
        type=msg_type,
        raw=obj,
        inner=data,
        status=status_payload,
        attributes=attributes_payload,
        request_id=str(request_id) if request_id is not None else None,
        mainboard_id=str(mainboard_id) if mainboard_id is not None else None,
    )
