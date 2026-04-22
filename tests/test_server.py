"""Tests for the FastAPI HTTP server.

Uses the existing ``_FakePrinter`` fixture from ``test_client`` to stand up
a local SDCP WebSocket server, then spins up the FastAPI app against it.
We hit the app through httpx's ASGI transport — no real HTTP port bound.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from pycentauri import server as server_module
from tests.test_client import MAINBOARD, _FakePrinter


async def _asgi_client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_read_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    # Mimic the lifespan manager manually: the async-context machinery
    # under httpx.AsyncClient doesn't drive lifespan, so do it ourselves.
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "pycentauri"
        assert body["printer_host"] == "127.0.0.1"

        r = await client.get("/status")
        assert r.status_code == 200
        s = r.json()
        assert s["print_status"] == 13
        assert s["temperatures"]["nozzle"]["actual"] == 210.0

        r = await client.get("/attributes")
        assert r.status_code == 200
        assert r.json()["mainboard_id"] == MAINBOARD

    await server.stop()


async def test_control_endpoints_404_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        for path in ("/print/start", "/print/pause", "/print/resume", "/print/stop"):
            r = await client.post(
                path, json={"filename": "x.gcode"} if path.endswith("start") else {}
            )
            assert r.status_code == 404, (
                f"control endpoint {path} should not exist without enable_control"
            )
    await server.stop()


async def test_control_endpoints_registered_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", enable_control=True, mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.post("/print/pause")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = await client.post("/print/resume")
        assert r.status_code == 200

    # The fake printer recorded the Cmd.PAUSE_PRINT + Cmd.RESUME_PRINT commands.
    from pycentauri.sdcp import Cmd

    cmds = [m["Data"]["Cmd"] for m in server.received]
    assert int(Cmd.PAUSE_PRINT) in cmds
    assert int(Cmd.RESUME_PRINT) in cmds
    await server.stop()


async def test_start_print_request_body_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", enable_control=True, mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.post("/print/start", json={})
        assert r.status_code == 422  # filename is required

        r = await client.post("/print/start", json={"filename": "cube.gcode"})
        assert r.status_code == 200
    await server.stop()
