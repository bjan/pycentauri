"""FastMCP server exposing the printer to MCP-speaking agents.

Register with an agent as a stdio server. Examples:

.. code-block:: sh

    # Read-only tools only:
    claude mcp add pycentauri -- python -m pycentauri.mcp

    # With control actions (start/pause/resume/stop):
    claude mcp add pycentauri -- python -m pycentauri.mcp --enable-control

The printer host is read from ``PYCENTAURI_HOST`` (preferred) or from a
``--host`` argument at launch time — it is **not** a per-tool parameter, so
an LLM cannot be tricked into targeting an arbitrary IP through prompt
injection.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from pycentauri.client import Printer
from pycentauri.discovery import discover as _lan_discover

_HOST_ENV = "PYCENTAURI_HOST"


def _resolve_host() -> str:
    host = os.environ.get(_HOST_ENV)
    if not host:
        raise RuntimeError(
            f"{_HOST_ENV} is not set; launch the server with --host IP or "
            "export PYCENTAURI_HOST first"
        )
    return host


def build_server(*, enable_control: bool = False) -> FastMCP:
    """Construct the FastMCP server, registering tools per the control flag.

    Control tools are not registered at all when ``enable_control=False`` —
    they never appear in the tool list the LLM sees.
    """
    mcp = FastMCP("pycentauri")

    @mcp.tool()
    async def get_status() -> dict[str, Any]:
        """Return the current printer status.

        Includes state code, job filename, progress %, layer, temperatures
        (nozzle / bed / chamber), fan speeds, and the raw SDCP payload.
        """
        host = _resolve_host()
        async with await Printer.connect(host) as printer:
            st = await printer.status()
        return {
            "host": host,
            "state": st.state,
            "print_status": st.print_status,
            "progress": st.progress,
            "filename": st.filename,
            "layer": {
                "current": st.print_info.current_layer if st.print_info else None,
                "total": st.print_info.total_layer if st.print_info else None,
            },
            "temperatures": {
                "nozzle": {"actual": st.temp_nozzle, "target": st.temp_nozzle_target},
                "bed": {"actual": st.temp_bed, "target": st.temp_bed_target},
                "chamber": {"actual": st.temp_chamber, "target": st.temp_chamber_target},
            },
            "position": st.coord,
            "z_offset": st.z_offset,
            "fans": st.fan_speed,
            "raw": st.raw,
        }

    @mcp.tool()
    async def get_attributes() -> dict[str, Any]:
        """Return printer attributes: model, firmware, mainboard ID, capabilities."""
        host = _resolve_host()
        async with await Printer.connect(host) as printer:
            attrs = await printer.attributes()
        return {
            "host": host,
            "mainboard_id": attrs.mainboard_id,
            "name": attrs.name,
            "machine_name": attrs.machine_name,
            "firmware_version": attrs.firmware_version,
            "capabilities": attrs.capabilities,
            "raw": attrs.raw,
        }

    @mcp.tool()
    async def get_snapshot() -> Image:
        """Return a JPEG snapshot of the built-in webcam.

        The image is returned inline so the agent can see what the printer
        is currently doing (e.g. to spot layer shifts or spaghetti).
        """
        host = _resolve_host()
        async with await Printer.connect(host) as printer:
            jpeg = await printer.snapshot()
        return Image(data=jpeg, format="jpeg")

    @mcp.tool()
    async def discover_printers() -> list[dict[str, Any]]:
        """Broadcast the SDCP discovery probe and return responding printers.

        Useful to verify the configured host matches what's actually on the
        LAN, or to find a newly-added printer's IP.
        """
        found = await _lan_discover(timeout=2.5)
        return [
            {
                "host": p.host,
                "mainboard_id": p.mainboard_id,
                "name": p.name,
                "machine_name": p.machine_name,
                "firmware_version": p.firmware_version,
            }
            for p in found
        ]

    if not enable_control:
        return mcp

    # --- Control tools (registered only when explicitly enabled) --------------

    @mcp.tool()
    async def start_print(
        filename: str,
        storage: str = "local",
        auto_leveling: bool = True,
        timelapse: bool = False,
    ) -> dict[str, Any]:
        """DESTRUCTIVE. Start a print of ``filename`` (already on the printer).

        Requires a file that has been uploaded to the printer. ``storage`` is
        ``"local"`` (default) or ``"udisk"``. Ask the user for confirmation
        before invoking — running a print unattended is the user's risk.
        """
        host = _resolve_host()
        async with await Printer.connect(host, enable_control=True) as printer:
            result = await printer.start_print(
                filename,
                storage=storage,
                auto_leveling=auto_leveling,
                timelapse=timelapse,
            )
        return {"ok": True, "response": result.inner}

    @mcp.tool()
    async def pause_print() -> dict[str, Any]:
        """DESTRUCTIVE. Pause the current print. Ask the user before invoking."""
        host = _resolve_host()
        async with await Printer.connect(host, enable_control=True) as printer:
            result = await printer.pause()
        return {"ok": True, "response": result.inner}

    @mcp.tool()
    async def resume_print() -> dict[str, Any]:
        """Resume a paused print."""
        host = _resolve_host()
        async with await Printer.connect(host, enable_control=True) as printer:
            result = await printer.resume()
        return {"ok": True, "response": result.inner}

    @mcp.tool()
    async def stop_print() -> dict[str, Any]:
        """DESTRUCTIVE. Stop the current print. Ask the user before invoking."""
        host = _resolve_host()
        async with await Printer.connect(host, enable_control=True) as printer:
            result = await printer.stop()
        return {"ok": True, "response": result.inner}

    return mcp


def run_stdio(*, enable_control: bool = False) -> None:
    """Run the MCP server over stdio until the transport closes."""
    mcp = build_server(enable_control=enable_control)
    mcp.run()


def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pycentauri.mcp",
        description="MCP server for pycentauri (stdio transport)",
    )
    parser.add_argument(
        "--enable-control",
        action="store_true",
        help="Register destructive tools (start/pause/resume/stop).",
    )
    parser.add_argument(
        "--host",
        help="Printer host/IP. Overrides $PYCENTAURI_HOST for this process.",
    )
    args = parser.parse_args()
    if args.host:
        os.environ[_HOST_ENV] = args.host
    run_stdio(enable_control=args.enable_control)


if __name__ == "__main__":
    _cli()
