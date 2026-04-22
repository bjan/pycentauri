"""Local-network toolkit for Elegoo Centauri Carbon 3D printers.

Six surfaces, all backed by the same async client:

* :class:`pycentauri.Printer` — asyncio SDCP client
* the ``centauri`` CLI (``python -m pycentauri.cli``)
* an MCP stdio server at ``python -m pycentauri.mcp``
* a FastAPI + SSE HTTP server at :mod:`pycentauri.server`
* a static web UI bundled in :mod:`pycentauri.web`
* an RTSP/H.264 bridge via MediaMTX in :mod:`pycentauri.rtsp`
"""

from pycentauri.client import ControlDisabledError, Printer, PrinterError
from pycentauri.discovery import DiscoveredPrinter, discover
from pycentauri.models import Attributes, PrintInfo, Status

__all__ = [
    "Attributes",
    "ControlDisabledError",
    "DiscoveredPrinter",
    "PrintInfo",
    "Printer",
    "PrinterError",
    "Status",
    "discover",
]

__version__ = "0.4.1"
