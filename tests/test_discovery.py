"""Loopback UDP test for the discovery probe."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest

from pycentauri.discovery import DISCOVERY_PROBE, discover


@pytest.fixture()
def fake_responder() -> tuple[socket.socket, int]:
    """Bind a UDP socket on localhost that replies to the SDCP probe."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    _, port = sock.getsockname()
    return sock, port


async def test_discover_parses_response(fake_responder: tuple[socket.socket, int]) -> None:
    sock, port = fake_responder

    async def responder() -> None:
        loop = asyncio.get_running_loop()
        sock.setblocking(False)
        while True:
            try:
                data, addr = await loop.sock_recvfrom(sock, 1024)
            except (asyncio.CancelledError, OSError):
                break
            if data != DISCOVERY_PROBE:
                continue
            reply = json.dumps(
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
            sock.sendto(reply, addr)

    task = asyncio.create_task(responder())
    try:
        # 1.5s budget with 3 retries — generous enough for macOS CI loopback.
        found = await discover(timeout=1.5, broadcast_address="127.0.0.1", port=port)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        sock.close()

    assert len(found) == 1
    p = found[0]
    assert p.mainboard_id == "ffffffff"
    assert p.machine_name == "Centauri Carbon"
    assert p.firmware_version == "V0.0.1"
