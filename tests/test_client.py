"""Client integration tests against an in-process fake SDCP WebSocket server.

These verify the subscribe-on-connect dance, mainboard-ID learning, status
routing, request/response correlation, and the ``enable_control`` gate,
without touching a real printer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import serve

from pycentauri.client import ControlDisabledError, Printer
from pycentauri.sdcp import Cmd

MAINBOARD = "abcdef123456"


class _FakePrinter:
    """In-process SDCP server just good enough for the client tests."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.port: int | None = None
        self._server = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws) -> None:  # type: ignore[no-untyped-def]
        # On connect: send an Attributes push so the client learns MainboardID
        await ws.send(
            json.dumps(
                {
                    "Id": MAINBOARD,
                    "Topic": f"sdcp/attributes/{MAINBOARD}",
                    "Data": {
                        "MainboardID": MAINBOARD,
                        "Attributes": {
                            "MainboardID": MAINBOARD,
                            "Name": "FakeCarbon",
                            "MachineName": "Centauri Carbon",
                            "FirmwareVersion": "V0.0.0",
                        },
                    },
                }
            )
        )

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self.received.append(msg)
            data = msg.get("Data") or {}
            cmd = data.get("Cmd")
            request_id = data.get("RequestID")

            # Ack the request.
            await ws.send(
                json.dumps(
                    {
                        "Id": MAINBOARD,
                        "Topic": f"sdcp/response/{MAINBOARD}",
                        "Data": {
                            "Cmd": cmd,
                            "RequestID": request_id,
                            "MainboardID": MAINBOARD,
                            "Data": {"Ack": 0},
                        },
                    }
                )
            )

            # After a subscribe, start pushing a minimal Status.
            if cmd == int(Cmd.SUBSCRIBE):
                t = asyncio.create_task(self._push_status(ws))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _push_status(self, ws) -> None:  # type: ignore[no-untyped-def]
        try:
            for i in range(3):
                await ws.send(
                    json.dumps(
                        {
                            "Id": MAINBOARD,
                            "Topic": f"sdcp/status/{MAINBOARD}",
                            "Data": {
                                "MainboardID": MAINBOARD,
                                "Status": {
                                    "CurrentStatus": [1],
                                    "TempOfNozzle": 210.0 + i,
                                    "TempOfHotbed": 60.0,
                                    "TempOfBox": 30.0,
                                    "TempTargetNozzle": 210,
                                    "TempTargetHotbed": 60,
                                    "TempTargetBox": 0,
                                    "PrintInfo": {
                                        "Status": 13,
                                        "Filename": "cube.gcode",
                                        "Progress": 10 + i,
                                        "CurrentLayer": i,
                                        "TotalLayer": 100,
                                    },
                                },
                            },
                        }
                    )
                )
                await asyncio.sleep(0.05)
        except Exception:
            pass


@asynccontextmanager
async def _fake_printer():
    server = _FakePrinter()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_status_and_attributes_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _fake_printer() as server:
        monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)
        async with await Printer.connect("127.0.0.1") as printer:
            attrs = await asyncio.wait_for(printer.attributes(), timeout=3)
            assert attrs.mainboard_id == MAINBOARD
            assert attrs.machine_name == "Centauri Carbon"

            status = await asyncio.wait_for(printer.status(), timeout=3)
            assert status.print_status == 13
            assert status.progress == 10
            assert status.temp_nozzle == 210.0

    # Subscribe (Cmd 512) must have been sent at least once.
    cmds = [m["Data"]["Cmd"] for m in server.received]
    assert int(Cmd.SUBSCRIBE) in cmds


async def test_watch_yields_multiple_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _fake_printer() as server:
        monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)
        async with await Printer.connect("127.0.0.1") as printer:
            seen: list[int | None] = []
            it = printer.watch()
            async for st in it:
                seen.append(st.progress)
                if len(seen) >= 3:
                    break
            await it.aclose()
        assert len(seen) >= 3


async def test_control_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _fake_printer() as server:
        monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)
        async with await Printer.connect("127.0.0.1") as printer:
            with pytest.raises(ControlDisabledError):
                await printer.start_print("cube.gcode")
            with pytest.raises(ControlDisabledError):
                await printer.stop()


async def test_preseeded_mainboard_skips_attributes_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the caller passes mainboard_id=, commands work before Attributes arrive.

    Simulates the paused-printer scenario: the fake server only pushes
    Attributes after it sees a subscribe, so a client that waits for
    Attributes before subscribing would deadlock. With mainboard_id=
    preset, the client can subscribe immediately.
    """

    class _SilentPrinter(_FakePrinter):
        async def _handler(self, ws) -> None:  # type: ignore[no-untyped-def,override]
            # Do NOT push Attributes automatically — only reply to commands.
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.received.append(msg)
                data = msg.get("Data") or {}
                cmd = data.get("Cmd")
                request_id = data.get("RequestID")
                await ws.send(
                    json.dumps(
                        {
                            "Id": MAINBOARD,
                            "Topic": f"sdcp/response/{MAINBOARD}",
                            "Data": {
                                "Cmd": cmd,
                                "RequestID": request_id,
                                "MainboardID": MAINBOARD,
                                "Data": {"Ack": 0},
                            },
                        }
                    )
                )

    server = _SilentPrinter()
    await server.start()
    try:
        monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)
        async with await Printer.connect(
            "127.0.0.1", enable_control=True, mainboard_id=MAINBOARD
        ) as printer:
            # Must work without any Attributes push ever arriving.
            result = await asyncio.wait_for(printer.pause(), timeout=3)
            assert result.inner is not None and result.inner["Data"]["Ack"] == 0
        assert any(m["Data"]["Cmd"] == int(Cmd.PAUSE_PRINT) for m in server.received)
    finally:
        await server.stop()


async def test_control_enabled_sends_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _fake_printer() as server:
        monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)
        async with await Printer.connect("127.0.0.1", enable_control=True) as printer:
            # Force mainboard learning to happen
            await asyncio.wait_for(printer.wait_for_mainboard(), timeout=2)
            result = await asyncio.wait_for(printer.pause(), timeout=3)
            assert result.inner is not None and result.inner["Data"]["Ack"] == 0

        cmds = [m["Data"]["Cmd"] for m in server.received]
        assert int(Cmd.PAUSE_PRINT) in cmds
