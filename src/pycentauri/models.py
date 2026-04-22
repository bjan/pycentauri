"""Typed views over SDCP status and attribute payloads.

The printer emits JSON with ``PascalCase`` keys and temperatures as
``[target, actual]`` pairs. These models present a Python-friendly facade
while keeping the raw dict around (``.raw``) for forward-compatibility when
the firmware adds fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PrintInfo(BaseModel):
    """Inner ``PrintInfo`` block describing the current print job.

    The printer mixes int and float for tick/time fields across firmware
    revisions, so the numeric fields here accept both.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: int | None = Field(default=None, alias="Status")
    filename: str | None = Field(default=None, alias="Filename")
    current_layer: int | None = Field(default=None, alias="CurrentLayer")
    total_layer: int | None = Field(default=None, alias="TotalLayer")
    current_ticks: float | None = Field(default=None, alias="CurrentTicks")
    total_ticks: float | None = Field(default=None, alias="TotalTicks")
    progress: int | None = Field(default=None, alias="Progress")
    err_num: int | None = Field(default=None, alias="ErrNum")
    print_speed: int | None = Field(default=None, alias="PrintSpeedPct")
    task_id: str | None = Field(default=None, alias="TaskId")


def _extract_temp(
    payload: dict[str, Any], actual_key: str, target_key: str
) -> tuple[float | None, float | None]:
    """Read a temperature field.

    Two wire formats exist in the wild:

    * Current Centauri Carbon firmware (V1.1.x): ``TempOfNozzle`` is a scalar
      and ``TempTargetNozzle`` is a separate scalar.
    * Older / CentauriLink-documented firmware: ``TempOfNozzle`` is a
      ``[target, actual]`` pair and no ``TempTarget*`` is sent.
    """
    raw_actual = payload.get(actual_key)
    raw_target = payload.get(target_key)
    actual: float | None = None
    target: float | None = None
    if isinstance(raw_actual, (int, float)):
        actual = float(raw_actual)
    elif isinstance(raw_actual, (list, tuple)) and len(raw_actual) >= 2:
        with _suppress_convert():
            target = float(raw_actual[0])
        with _suppress_convert():
            actual = float(raw_actual[1])
    if target is None and isinstance(raw_target, (int, float)):
        target = float(raw_target)
    return actual, target


class _suppress_convert:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, (TypeError, ValueError))


def _parse_coord(raw: Any) -> tuple[float, float, float] | None:
    """Parse a ``"x,y,z"`` coord string into a tuple of floats."""
    if not isinstance(raw, str):
        return None
    parts = raw.split(",")
    if len(parts) != 3:
        return None
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (TypeError, ValueError):
        return None


class Status(BaseModel):
    """Normalised view of the ``Status`` payload from an SDCP push.

    The raw payload is always kept on ``.raw`` — new firmware fields show up
    there automatically even if we haven't added a typed accessor yet.
    """

    model_config = ConfigDict(extra="allow")

    raw: dict[str, Any]
    current_status: list[int] = Field(default_factory=list)
    print_info: PrintInfo | None = None

    temp_nozzle: float | None = None
    temp_nozzle_target: float | None = None
    temp_bed: float | None = None
    temp_bed_target: float | None = None
    temp_chamber: float | None = None
    temp_chamber_target: float | None = None

    coord: tuple[float, float, float] | None = None
    z_offset: float | None = None
    fan_speed: dict[str, int] = Field(default_factory=dict)
    light: dict[str, Any] = Field(default_factory=dict)
    time_lapse: int | None = None
    platform_type: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Status:
        pi_raw = payload.get("PrintInfo")
        print_info = PrintInfo.model_validate(pi_raw) if isinstance(pi_raw, dict) else None

        current_status_raw = payload.get("CurrentStatus") or payload.get("CurrenStatus") or []
        current_status = (
            [int(x) for x in current_status_raw if isinstance(x, (int, float))]
            if isinstance(current_status_raw, list)
            else []
        )

        noz, noz_t = _extract_temp(payload, "TempOfNozzle", "TempTargetNozzle")
        bed, bed_t = _extract_temp(payload, "TempOfHotbed", "TempTargetHotbed")
        box, box_t = _extract_temp(payload, "TempOfBox", "TempTargetBox")

        fans_raw = payload.get("CurrentFanSpeed") or {}
        fans = {
            str(k): int(v)
            for k, v in (fans_raw.items() if isinstance(fans_raw, dict) else [])
            if isinstance(v, (int, float))
        }

        light_raw = payload.get("LightStatus") or {}
        light = light_raw if isinstance(light_raw, dict) else {}

        z_off = payload.get("ZOffset")
        z_off_f = float(z_off) if isinstance(z_off, (int, float)) else None

        return cls(
            raw=payload,
            current_status=current_status,
            print_info=print_info,
            temp_nozzle=noz,
            temp_nozzle_target=noz_t,
            temp_bed=bed,
            temp_bed_target=bed_t,
            temp_chamber=box,
            temp_chamber_target=box_t,
            coord=_parse_coord(payload.get("CurrenCoord") or payload.get("CurrentCoord")),
            z_offset=z_off_f,
            fan_speed=fans,
            light=light,
            time_lapse=payload.get("TimeLapseStatus"),
            platform_type=payload.get("PlatFormType") or payload.get("PlatformType"),
        )

    @property
    def state(self) -> int | None:
        """Primary printer state code (first entry in ``CurrentStatus``)."""
        return self.current_status[0] if self.current_status else None

    @property
    def progress(self) -> int | None:
        """Convenience accessor for the current job's progress (%)."""
        return self.print_info.progress if self.print_info else None

    @property
    def filename(self) -> str | None:
        return self.print_info.filename if self.print_info else None

    @property
    def print_status(self) -> int | None:
        """The ``PrintInfo.Status`` code (e.g. 13 = printing). See :class:`PrintStatus`."""
        return self.print_info.status if self.print_info else None


class PrintStatus:
    """``PrintInfo.Status`` codes from the official Elegoo SDK.

    Sourced verbatim from
    ``src/lan/adapters/elegoo_fdm_cc/elegoo_fdm_cc_message_adapter.cpp``
    in the ``ELEGOO-3D/elegoo-link`` repository. Codes in the 2-4 and 23-26
    ranges are resin-printer / LCD-specific and typically aren't surfaced
    by the Centauri Carbon, but are kept for forward-compatibility.
    """

    IDLE = 0
    HOMING = 1
    DROPPING = 2
    EXPOSING = 3
    LIFTING = 4
    PAUSING = 5
    PAUSED = 6
    STOPPING = 7
    STOPPED = 8
    COMPLETED = 9
    FILE_CHECKING = 10
    PRINTER_CHECKING = 11
    RESUMING = 12
    PRINTING = 13
    ERROR = 14
    AUTO_LEVELING = 15
    PREHEATING = 16
    RESONANCE_TESTING = 17
    PRINT_START = 18
    AUTO_LEVELING_COMPLETED = 19
    PREHEATING_COMPLETED = 20
    HOMING_COMPLETED = 21
    RESONANCE_TESTING_COMPLETED = 22
    AUTO_FEEDING = 23
    UNLOADING = 24
    UNLOADING_ABNORMAL = 25
    UNLOADING_PAUSED = 26


class Attributes(BaseModel):
    """Normalised view of the ``Attributes`` payload."""

    model_config = ConfigDict(extra="allow")

    raw: dict[str, Any]
    mainboard_id: str | None = None
    name: str | None = None
    machine_name: str | None = None
    brand_name: str | None = None
    firmware_version: str | None = None
    protocol_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Attributes:
        caps_raw = payload.get("Capabilities") or payload.get("SupportedFeatures") or []
        caps = [str(c) for c in caps_raw] if isinstance(caps_raw, list) else []
        return cls(
            raw=payload,
            mainboard_id=payload.get("MainboardID"),
            name=payload.get("Name"),
            machine_name=payload.get("MachineName"),
            brand_name=payload.get("BrandName"),
            firmware_version=payload.get("FirmwareVersion"),
            protocol_version=payload.get("ProtocolVersion"),
            capabilities=caps,
        )
