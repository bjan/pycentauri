"""Local-network toolkit for Elegoo Centauri Carbon 3D printers.

Six surfaces, all backed by the same async client:

* :class:`pycentauri.Printer` — asyncio SDCP client
* the ``centauri`` CLI (``python -m pycentauri.cli``)
* an MCP stdio server at ``python -m pycentauri.mcp``
* a FastAPI + SSE HTTP server at :mod:`pycentauri.server`
* a static web UI bundled in :mod:`pycentauri.web`
* an RTSP/H.264 bridge via MediaMTX in :mod:`pycentauri.rtsp`
"""

from pycentauri.cc2 import CC2Printer
from pycentauri.client import ControlDisabledError, Printer, PrinterError
from pycentauri.connect import connect_auto
from pycentauri.discovery import DiscoveredPrinter, discover
from pycentauri.models import (
    Attributes,
    CanvasStatus,
    CanvasTray,
    CanvasUnit,
    PrintInfo,
    Status,
)

__all__ = [
    "Attributes",
    "CC2Printer",
    "CanvasStatus",
    "CanvasTray",
    "CanvasUnit",
    "ControlDisabledError",
    "DiscoveredPrinter",
    "PrintInfo",
    "Printer",
    "PrinterError",
    "Status",
    "connect_auto",
    "discover",
]

__version__ = "0.6.5"
