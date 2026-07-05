"""Auto-detect CC1 vs CC2 and return the right Printer subclass."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pycentauri.client import Printer, PrinterError

log = logging.getLogger(__name__)


async def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        w.close()
        await w.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def connect_auto(
    host: str,
    *,
    enable_control: bool = False,
    connect_timeout: float = 10.0,
    mainboard_id: str | None = None,
    access_code: str | None = None,
    **kwargs: Any,
) -> Printer:
    """Connect to a printer, auto-detecting CC1 (WebSocket :3030) vs CC2 (MQTT :1883).

    Returns a ``Printer`` for CC1 or ``CC2Printer`` for CC2 — both expose
    the same public API, so callers don't need to care which one they got.

    ``access_code`` is required for CC2 (the printer's API key). It is
    ignored for CC1 (which has no auth).

    Detection probes only :1883. A CC1 answers that with an instant kernel
    RST (its fragile ``app`` daemon never sees the probe), and the CC1 path
    then uses the real WebSocket connect as its own probe — no throwaway
    open/close cycles ever hit :3030, the port that's sensitive to
    connect/close churn.
    """
    if await _port_open(host, 1883, timeout=min(connect_timeout / 2, 3.0)):
        from pycentauri.cc2 import CC2Printer

        if not access_code:
            raise PrinterError(
                f"{host} appears to be a CC2 (MQTT :1883 open) but no "
                "access_code was provided. Pass --access-code or set "
                "PYCENTAURI_ACCESS_CODE."
            )
        log.info("detected CC2 (MQTT :1883) at %s", host)
        return await CC2Printer.connect(
            host,
            enable_control=enable_control,
            access_code=access_code,
            connect_timeout=connect_timeout,
            mainboard_id=mainboard_id,
            **kwargs,
        )

    try:
        printer = await Printer.connect(
            host,
            enable_control=enable_control,
            connect_timeout=connect_timeout,
            mainboard_id=mainboard_id,
            **kwargs,
        )
    except (OSError, asyncio.TimeoutError) as err:
        raise PrinterError(
            f"cannot connect to {host}: :1883 (CC2/MQTT) is closed and "
            f":3030 (CC1/SDCP WebSocket) did not answer ({err!r})"
        ) from err
    log.info("connected to CC1 (WebSocket :3030) at %s", host)
    return printer
