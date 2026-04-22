"""LAN discovery for Elegoo Centauri Carbon printers.

The original Centauri Carbon listens on UDP port 3000 and responds to the
magic probe string ``M99999`` with a JSON payload describing itself. The
newer Centauri Carbon 2 uses a different JSON-RPC probe and is not supported
here.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any

DISCOVERY_PORT = 3000
DISCOVERY_PROBE = b"M99999"
DEFAULT_TIMEOUT = 3.0


@dataclass(slots=True)
class DiscoveredPrinter:
    """A printer that answered a discovery broadcast."""

    host: str
    mainboard_id: str | None
    name: str | None
    machine_name: str | None
    firmware_version: str | None
    raw: dict[str, Any]


def _parse_response(data: bytes, host: str) -> DiscoveredPrinter | None:
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    inner_raw = obj.get("Data")
    inner: dict[str, Any] = inner_raw if isinstance(inner_raw, dict) else {}
    return DiscoveredPrinter(
        host=host,
        mainboard_id=inner.get("MainboardID") or obj.get("MainboardID"),
        name=inner.get("Name"),
        machine_name=inner.get("MachineName"),
        firmware_version=inner.get("FirmwareVersion"),
        raw=obj,
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.results: dict[str, DiscoveredPrinter] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        host = addr[0]
        if host in self.results:
            return
        parsed = _parse_response(data, host)
        if parsed is not None:
            self.results[host] = parsed


async def discover(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    broadcast_address: str = "255.255.255.255",
    port: int = DISCOVERY_PORT,
) -> list[DiscoveredPrinter]:
    """Broadcast the SDCP discovery probe and collect responders.

    Blocks for ``timeout`` seconds. Returns one entry per responding printer,
    de-duplicated by source IP. Safe to call concurrently from multiple
    tasks; each call uses its own UDP socket.
    """
    loop = asyncio.get_running_loop()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    sock.setblocking(False)

    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        sock=sock,
    )
    try:
        transport.sendto(DISCOVERY_PROBE, (broadcast_address, port))
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    return list(protocol.results.values())
