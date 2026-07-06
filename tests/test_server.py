"""Tests for the FastAPI HTTP server.

Uses the existing ``_FakePrinter`` fixture from ``test_client`` to stand up
a local SDCP WebSocket server, then spins up the FastAPI app against it.
We hit the app through httpx's ASGI transport — no real HTTP port bound.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from pycentauri import server as server_module
from pycentauri.client import Printer
from tests.test_client import MAINBOARD, _FakePrinter


@pytest.fixture(autouse=True)
def _bypass_connect_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the server use Printer.connect directly, skipping port detection."""

    async def _direct_connect(host, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("access_code", None)
        return await Printer.connect(host, **kwargs)

    monkeypatch.setattr("pycentauri.server.connect_auto", _direct_connect)


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
        # /api/info is the JSON health endpoint; / redirects to /ui/.
        r = await client.get("/api/info")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "pycentauri"
        assert body["printer_host"] == "127.0.0.1"

        r = await client.get("/ui/", follow_redirects=False)
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text

        r = await client.get("/status")
        assert r.status_code == 200
        s = r.json()
        assert s["print_status"] == 13
        assert s["temperatures"]["nozzle"]["actual"] == 210.0

        r = await client.get("/attributes")
        assert r.status_code == 200
        assert r.json()["mainboard_id"] == MAINBOARD

    await server.stop()


async def test_openapi_schema_generates(monkeypatch: pytest.MonkeyPatch) -> None:
    # /openapi.json (and thus /docs) must render. Regression for a request
    # body model defined inside create_app(), whose forward refs couldn't
    # be resolved under `from __future__ import annotations`. Control on so
    # every body model — including the Canvas RefillBody — is registered.
    app = server_module.create_app("127.0.0.1", enable_control=True, mainboard_id=MAINBOARD)
    async with await _asgi_client(app) as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["title"]


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


async def test_adjust_endpoints_registered_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", enable_control=True, mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.post("/print/speed", json={"mode": "sport"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = await client.post("/print/fan", json={"model": 30, "chamber": 50})
        assert r.status_code == 200

        r = await client.post("/print/temperature", json={"nozzle": 200, "bed": 55})
        assert r.status_code == 200

        # No-target → 400 from the library
        r = await client.post("/print/fan", json={})
        assert r.status_code == 400

        # Pydantic-level bounds (≥0, ≤100) → 422
        r = await client.post("/print/fan", json={"model": 150})
        assert r.status_code == 422

    from pycentauri.sdcp import Cmd as _Cmd

    sent = [m["Data"] for m in server.received if m["Data"]["Cmd"] == int(_Cmd.CHANGE_PRINT_PARAMS)]
    assert len(sent) == 3
    speed, fan, temp = sent
    assert speed["Data"] == {"PrintSpeedPct": 130}
    assert fan["Data"] == {"TargetFanSpeed": {"ModelFan": 30, "BoxFan": 50}}
    assert temp["Data"] == {"TempTargetNozzle": 200.0, "TempTargetHotbed": 55.0}
    await server.stop()


async def test_adjust_endpoints_404_without_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        for path in ("/print/speed", "/print/fan", "/print/temperature"):
            r = await client.post(path, json={})
            assert r.status_code == 404, path
    await server.stop()


async def test_rtsp_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.get("/api/rtsp")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["running"] is False

        # Start/stop should 404 when feature is off.
        r = await client.post("/api/rtsp/start")
        assert r.status_code == 404
    await server.stop()


async def test_rtsp_enabled_reports_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Don't actually spawn mediamtx — point the override at a fake binary so
    # the availability check passes for /api/rtsp/start's "could not launch"
    # path to be exercised, then end with a real stop() no-op.
    fake_mtx = tmp_path / "mediamtx"
    fake_mtx.write_text("#!/bin/sh\nsleep 60\n")
    fake_mtx.chmod(0o755)
    fake_ffmpeg = tmp_path / "ffmpeg"
    fake_ffmpeg.write_text("#!/bin/sh\nsleep 60\n")
    fake_ffmpeg.chmod(0o755)

    from pycentauri.rtsp import RtspConfig

    cfg = RtspConfig(
        printer_host="192.168.1.209",
        rtsp_port=18554,
        bind="127.0.0.1",
        path="printer",
        mediamtx_path=str(fake_mtx),
        ffmpeg_path=str(fake_ffmpeg),
    )

    fake_ws = _FakePrinter()
    await fake_ws.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", fake_ws.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD, rtsp_config=cfg)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.get("/api/rtsp")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["available"] is True
        assert body["running"] is False
        assert body["port"] == 18554
        assert body["path"] == "printer"
        assert body["urls"] == ["rtsp://127.0.0.1:18554/printer"]

        # Start will spawn the fake binary (which just sleeps).
        r = await client.post("/api/rtsp/start")
        assert r.status_code == 200
        assert r.json()["running"] is True

        # Stop cleans up.
        r = await client.post("/api/rtsp/stop")
        assert r.status_code == 200
        assert r.json()["running"] is False
    await fake_ws.stop()


async def test_rtsp_unavailable_when_binaries_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pycentauri.rtsp import RtspConfig

    cfg = RtspConfig(
        printer_host="192.168.1.209",
        mediamtx_path=str(tmp_path / "does-not-exist"),
    )

    fake_ws = _FakePrinter()
    await fake_ws.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", fake_ws.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD, rtsp_config=cfg)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.get("/api/rtsp")
        body = r.json()
        assert body["enabled"] is True
        assert body["available"] is False
        assert body["running"] is False
        assert "MediaMTX" in (body["reason"] or "")

        # Start should 503 with an install hint.
        r = await client.post("/api/rtsp/start")
        assert r.status_code == 503
        assert "MediaMTX" in r.text
    await fake_ws.stop()


async def test_canvas_unsupported_maps_to_501(monkeypatch: pytest.MonkeyPatch) -> None:
    """CC1 has no Canvas: GET /canvas must be 501 (so the UI stops asking),
    reserving 504 for transient timeouts on a real CC2."""
    server = _FakePrinter()
    await server.start()
    monkeypatch.setattr("pycentauri.client.WS_PORT", server.port)

    app = server_module.create_app("127.0.0.1", mainboard_id=MAINBOARD)
    async with app.router.lifespan_context(app), await _asgi_client(app) as client:
        r = await client.get("/canvas")
        assert r.status_code == 501
        assert "CC1" in r.json()["detail"]
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
