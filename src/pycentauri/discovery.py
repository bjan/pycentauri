"""LAN discovery for Elegoo Centauri Carbon printers.

The original Centauri Carbon (CC1) listens on UDP port 3000 and responds to
the magic probe string ``M99999`` with a JSON payload describing itself. The
Centauri Carbon 2 (CC2) listens on UDP port 52700 and responds to a
JSON-RPC probe (``{"id": 0, "method": 7000}``) with its own JSON payload.
Both probes are broadcast from the same socket, and responses are told apart
by shape: CC1 replies nest their fields under ``Data``, CC2 replies nest
theirs under ``result``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any

DISCOVERY_PORT = 3000
DISCOVERY_PROBE = b"M99999"
DISCOVERY_PORT_CC2 = 52700
DISCOVERY_PROBE_CC2 = json.dumps({"id": 0, "method": 7000}).encode("utf-8")
DEFAULT_TIMEOUT = 3.0


@dataclass(slots=True)
class DiscoveredPrinter:
    """A printer that answered a discovery broadcast."""

    host: str
    protocol: str  # "cc1" or "cc2"
    mainboard_id: str | None
    name: str | None
    machine_name: str | None
    firmware_version: str | None
    serial_number: str | None
    lan_status: int | None
    raw: dict[str, Any]


def _parse_response(data: bytes, host: str) -> DiscoveredPrinter | None:
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None

    result_raw = obj.get("result")
    if isinstance(result_raw, dict) and "sn" in result_raw:
        return DiscoveredPrinter(
            host=host,
            protocol="cc2",
            mainboard_id=None,
            name=result_raw.get("host_name"),
            machine_name=result_raw.get("machine_model"),
            firmware_version=None,
            serial_number=result_raw.get("sn"),
            lan_status=result_raw.get("lan_status"),
            raw=obj,
        )

    inner_raw = obj.get("Data")
    inner: dict[str, Any] = inner_raw if isinstance(inner_raw, dict) else {}
    return DiscoveredPrinter(
        host=host,
        protocol="cc1",
        mainboard_id=inner.get("MainboardID") or obj.get("MainboardID"),
        name=inner.get("Name"),
        machine_name=inner.get("MachineName"),
        firmware_version=inner.get("FirmwareVersion"),
        serial_number=None,
        lan_status=None,
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
    port_cc2: int = DISCOVERY_PORT_CC2,
    retries: int = 3,
) -> list[DiscoveredPrinter]:
    """Broadcast both the CC1 and CC2 discovery probes and collect responders.

    Blocks for ``timeout`` seconds. Returns one entry per responding printer,
    de-duplicated by source IP. Each probe is retransmitted ``retries`` times
    at evenly-spaced intervals within the timeout window, since UDP probes
    can be dropped on busy or congested networks. Both probes are sent from
    the same socket, since replies come back to the sender's address:port
    regardless of which port they were sent to. Safe to call concurrently
    from multiple tasks; each call uses its own UDP socket.
    """
    loop = asyncio.get_running_loop()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)

    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        sock=sock,
    )
    try:
        tries = max(1, retries)
        interval = timeout / max(tries, 1) / 2
        for _ in range(tries):
            transport.sendto(DISCOVERY_PROBE, (broadcast_address, port))
            transport.sendto(DISCOVERY_PROBE_CC2, (broadcast_address, port_cc2))
            await asyncio.sleep(interval)
        # Listen for the remainder of the budget for late replies.
        remaining = max(0.0, timeout - interval * tries)
        if remaining:
            await asyncio.sleep(remaining)
    finally:
        transport.close()

    return list(protocol.results.values())
