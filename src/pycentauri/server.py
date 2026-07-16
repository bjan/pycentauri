"""FastAPI HTTP server exposing the printer over REST + SSE.

A single long-lived connection (WebSocket for CC1, MQTT for CC2 — chosen
by :func:`pycentauri.connect_auto`) is held for the lifetime of the
server and reused across requests, staying well under CC1's 5-slot
limit. On connection errors a supervisor reconnects in the background
with exponential backoff.

Routes (start with ``centauri server``):

* ``GET /`` — redirect to ``/ui/`` (the web dashboard)
* ``GET /api/info`` — health, version, and connection state (JSON)
* ``GET /status`` / ``GET /attributes`` — printer state (JSON)
* ``GET /snapshot`` / ``GET /stream`` — webcam JPEG / MJPEG proxy
* ``GET /events/status`` — Server-Sent Events stream of status pushes
* ``GET /discover`` — UDP LAN scan (finds CC1s)
* ``GET /canvas`` — Canvas multi-filament state (CC2)
* ``GET /files`` / ``GET /disk`` / ``GET /history`` — file list, disk usage
  (CC2), and print history (both models)
* ``GET|POST /api/rtsp*`` — RTSP bridge state/control (with ``--rtsp``)
* ``POST /print/{start,pause,resume,stop,speed,fan,temperature}``,
  ``POST /canvas/refill``, ``POST /light``, ``POST /files/upload``, and
  ``POST /files/delete`` — registered only with ``--enable-control``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from pycentauri import __version__, mjpeg_broadcast
from pycentauri import rtsp as rtsp_module
from pycentauri.camera import CAMERA_PATH
from pycentauri.client import (
    ControlDisabledError,
    Printer,
    PrinterError,
    RequestTimeoutError,
)
from pycentauri.connect import connect_auto
from pycentauri.discovery import DiscoveredPrinter
from pycentauri.discovery import discover as lan_discover

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_START = 1.0
RECONNECT_BACKOFF_MAX = 30.0


class PrinterManager:
    """Owns a single long-lived :class:`Printer` connection for the whole server.

    Runs a background supervisor that keeps a connection open, reconnects
    with exponential backoff on failure, and re-subscribes to status pushes
    after each reconnect. The Printer instance is exposed via :attr:`printer`
    for tool use — callers that need write operations should pass
    ``enable_control=True`` at construction.
    """

    def __init__(
        self,
        host: str,
        *,
        enable_control: bool = False,
        mainboard_id: str | None = None,
        access_code: str | None = None,
        rtsp_config: rtsp_module.RtspConfig | None = None,
    ) -> None:
        self.host = host
        self.enable_control = enable_control
        self.access_code = access_code
        self._mainboard_id = mainboard_id
        self._rtsp_config = rtsp_config
        self._printer: Printer | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = False
        # One shared upstream camera connection for all browsers — the
        # printer's MJPEG server starves under connection churn.
        self.camera = mjpeg_broadcast.CameraBroadcaster(
            lambda: f"http://{self.host}:{self.printer.camera_port}{CAMERA_PATH}"
        )

    @property
    def printer(self) -> Printer:
        """The currently active Printer. Raises if not connected."""
        if self._printer is None or self._printer._closed:
            raise HTTPException(status_code=503, detail="printer connection not ready")
        return self._printer

    async def start(self) -> None:
        """Launch the supervisor task and wait for the first connection."""
        self._supervisor = asyncio.create_task(self._run(), name="pycentauri-supervisor")
        # Don't block startup indefinitely; server is still useful even while
        # reconnecting (endpoints will 503 cleanly).
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)

    async def stop(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            await self.camera.close()
        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
        if self._printer is not None:
            with contextlib.suppress(Exception):
                await self._printer.close()

    async def _run(self) -> None:
        backoff = RECONNECT_BACKOFF_START
        while not self._closing:
            # Learn the mainboard via discovery if we don't have one yet.
            if self._mainboard_id is None:
                found = await lan_discover(timeout=1.5, retries=2)
                for p in found:
                    if p.host == self.host and p.mainboard_id:
                        self._mainboard_id = p.mainboard_id
                        log.info("learned mainboard id %s", self._mainboard_id)
                        break

            try:
                self._printer = await connect_auto(
                    self.host,
                    enable_control=self.enable_control,
                    mainboard_id=self._mainboard_id,
                    access_code=self.access_code,
                )
                # CC1: prime subscription so status pushes flow.
                # CC2: no-op (status is polled or pushed via MQTT).
                if hasattr(self._printer, "_ensure_subscribed"):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._printer._ensure_subscribed(),
                            timeout=5.0,
                        )
                log.info("connected to %s", self.host)
                # The MJPEG port differs per model (CC1 :3031, CC2 :8080)
                # and we only know which we got after connecting.
                if self._rtsp_config is not None:
                    self._rtsp_config.camera_port = self._printer.camera_port
                self._ready.set()
                backoff = RECONNECT_BACKOFF_START
                # CC1: hold until the WS reader dies (disconnect).
                # CC2: hold until the MQTT loop ends (or we close).
                reader = getattr(self._printer, "_reader", None)
                if reader is not None:
                    await reader
                else:
                    # CC2: just sleep until closed; the MQTT loop_start
                    # thread handles reconnects internally.
                    while not self._closing and not self._printer._closed:
                        await asyncio.sleep(2.0)
            except Exception as e:
                log.warning("connection to %s failed: %r", self.host, e)

            if self._closing:
                break
            self._ready.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


class RtspController:
    """Start / stop a MediaMTX subprocess on demand from the HTTP server.

    Designed to be held on ``app.state`` alongside the PrinterManager and
    driven by the web UI. Idempotent: multiple start calls while already
    running are a no-op; stop on a stopped server is a no-op.
    """

    def __init__(self, cfg: rtsp_module.RtspConfig) -> None:
        self.cfg = cfg
        self._proc: subprocess.Popen[bytes] | None = None
        self._cfg_path: str | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def urls(self, advertised_host: str | None = None) -> list[str]:
        return rtsp_module.build_urls(self.cfg, advertised_host=advertised_host)

    def available(self) -> bool:
        try:
            rtsp_module.ensure_binaries(self.cfg)
            return True
        except rtsp_module.RtspError:
            return False

    def unavailable_reason(self) -> str | None:
        try:
            rtsp_module.ensure_binaries(self.cfg)
            return None
        except rtsp_module.RtspError as err:
            return str(err)

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            # The subprocess spawn itself is fast but blocks briefly; run it
            # in a thread so we don't stall the event loop.
            loop = asyncio.get_running_loop()
            try:
                proc, cfg_path = await loop.run_in_executor(
                    None, rtsp_module.start_detached, self.cfg
                )
            except rtsp_module.RtspError as err:
                self._last_error = str(err)
                raise
            self._proc = proc
            self._cfg_path = cfg_path
            self._last_error = None

    async def stop(self) -> None:
        async with self._lock:
            proc, cfg_path = self._proc, self._cfg_path
            self._proc = None
            self._cfg_path = None
        if proc is None and cfg_path is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, rtsp_module.stop_detached, proc, cfg_path)


# --- Pydantic request bodies ------------------------------------------------


class StartPrintBody(BaseModel):
    filename: str = Field(..., description="File name as it appears on the printer.")
    storage: str = Field("local", description="'local' or 'udisk'.")
    auto_leveling: bool = True
    timelapse: bool = False


class PrintSpeedBody(BaseModel):
    """Print-speed mode. Firmware accepts only 4 discrete values."""

    mode: str | int = Field(
        ...,
        description=(
            "Mode name ('silent'|'balanced'|'sport'|'ludicrous') or "
            "the corresponding PrintSpeedPct value (50|100|130|160)."
        ),
    )


class FanSpeedBody(BaseModel):
    model: int | None = Field(None, ge=0, le=100)
    auxiliary: int | None = Field(None, ge=0, le=100)
    chamber: int | None = Field(None, ge=0, le=100)


class TemperatureBody(BaseModel):
    nozzle: float | None = Field(None, ge=0, le=300)
    bed: float | None = Field(None, ge=0, le=110)
    chamber: float | None = Field(None, ge=0, le=60)


class RefillBody(BaseModel):
    enabled: bool = Field(..., description="true to enable auto-refill, false to disable.")


class LightBody(BaseModel):
    on: bool = Field(..., description="true to turn the chamber light on, false for off.")


# --- Dependency helpers -----------------------------------------------------


def get_manager(request: Request) -> PrinterManager:
    mgr: PrinterManager | None = getattr(request.app.state, "manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="printer manager not initialised")
    return mgr


def require_control(manager: PrinterManager = Depends(get_manager)) -> PrinterManager:
    if not manager.enable_control:
        raise HTTPException(
            status_code=403,
            detail=(
                "control actions are disabled. Launch the server with "
                "--enable-control to enable POST /print/* endpoints."
            ),
        )
    return manager


# --- App factory ------------------------------------------------------------


# --- optional PyPI update check (opt-in via --check-updates) ----------------

PYPI_JSON_URL = "https://pypi.org/pypi/pycentauri/json"
UPDATE_CHECK_INTERVAL_S = 12 * 3600


class _UpdateState:
    """The latest version seen on PyPI, or ``None`` until a successful check."""

    def __init__(self) -> None:
        self.latest: str | None = None


def _update_available(current: str, latest: str | None) -> bool:
    """True when ``latest`` (from PyPI) is a newer release than ``current``.

    Uses PEP 440 parsing so a locally-run dev build that's *ahead* of PyPI
    (e.g. between commit and publish) never shows a spurious "update".
    """
    if not latest:
        return False
    try:
        from packaging.version import InvalidVersion, parse

        try:
            return parse(latest) > parse(current)
        except InvalidVersion:
            return False
    except Exception:
        return False


async def _update_check_loop(state: _UpdateState) -> None:
    """Poll PyPI for the latest version on an interval. Fail-silent.

    This is the single outbound (non-printer) call in the whole server, and
    it runs only when ``--check-updates`` is set. It reads a version string
    and sends nothing about the user or the printer.
    """
    import httpx

    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(PYPI_JSON_URL, headers={"Accept": "application/json"})
            if resp.status_code == 200:
                latest = (resp.json().get("info") or {}).get("version")
                if isinstance(latest, str):
                    state.latest = latest
        except Exception:
            pass  # network down / PyPI hiccup — try again next interval
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)


def create_app(
    host: str,
    *,
    enable_control: bool = False,
    mainboard_id: str | None = None,
    access_code: str | None = None,
    rtsp_config: rtsp_module.RtspConfig | None = None,
    check_updates: bool = False,
) -> FastAPI:
    """Build the FastAPI app. ``host`` is the printer's IP/hostname.

    The app runs a single background :class:`PrinterManager` that owns the
    connection lifecycle (WebSocket for CC1, MQTT for CC2 — auto-detected).
    Control endpoints are registered only when ``enable_control`` is ``True``.
    ``rtsp_config`` enables the ``/api/rtsp/*`` endpoints and the "STREAM"
    panel in the web UI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager = PrinterManager(
            host,
            enable_control=enable_control,
            mainboard_id=mainboard_id,
            access_code=access_code,
            rtsp_config=rtsp_config,
        )
        app.state.manager = manager
        app.state.rtsp = RtspController(rtsp_config) if rtsp_config is not None else None
        app.state.update = _UpdateState()
        update_task: asyncio.Task[None] | None = None
        if check_updates:
            update_task = asyncio.create_task(
                _update_check_loop(app.state.update), name="pycentauri-update-check"
            )
        await manager.start()
        try:
            yield
        finally:
            if update_task is not None:
                update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await update_task
            if app.state.rtsp is not None:
                with contextlib.suppress(Exception):
                    await app.state.rtsp.stop()
            await manager.stop()

    app = FastAPI(
        title="pycentauri HTTP API",
        version=__version__,
        description=(
            "HTTP + SSE surface for an Elegoo Centauri Carbon 3D printer. "
            "Wrapping the same client library that powers the `centauri` "
            "CLI and the MCP server."
        ),
        lifespan=lifespan,
    )

    @app.get("/status", tags=["read"])
    async def status_endpoint(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        try:
            st = await asyncio.wait_for(manager.printer.status(), timeout=10)
        except asyncio.TimeoutError as err:
            raise HTTPException(status_code=504, detail="printer status timeout") from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {
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

    @app.get("/attributes", tags=["read"])
    async def attributes_endpoint(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        try:
            attrs = await asyncio.wait_for(manager.printer.attributes(), timeout=10)
        except asyncio.TimeoutError as err:
            raise HTTPException(status_code=504, detail="printer attributes timeout") from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {
            "mainboard_id": attrs.mainboard_id,
            "name": attrs.name,
            "machine_name": attrs.machine_name,
            "firmware_version": attrs.firmware_version,
            "capabilities": attrs.capabilities,
            "raw": attrs.raw,
        }

    @app.get("/snapshot", tags=["read"], response_class=Response)
    async def snapshot_endpoint(
        manager: PrinterManager = Depends(get_manager),
    ) -> Response:
        try:
            jpeg = await asyncio.wait_for(manager.printer.snapshot(), timeout=15)
        except Exception as err:
            raise HTTPException(status_code=502, detail=f"snapshot failed: {err}") from err
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/stream", tags=["read"])
    async def stream_endpoint(
        manager: PrinterManager = Depends(get_manager),
    ) -> StreamingResponse:
        """Proxy the printer's MJPEG stream.

        The printer serves ``multipart/x-mixed-replace`` (CC1 :3031,
        CC2 :8080). Browsers render it directly in an ``<img src=…>``.
        Every browser here attaches to a *single* shared upstream
        connection (see :class:`CameraBroadcaster`): the printer's camera
        server starves under connection churn, so no matter how many tabs
        or reloads hit ``/stream``, the printer only ever sees one.
        """
        try:
            media_type, chunks = await manager.camera.subscribe()
        except mjpeg_broadcast.CameraUnavailable as err:
            raise HTTPException(status_code=502, detail=f"webcam unavailable: {err}") from err
        return StreamingResponse(chunks, media_type=media_type)

    @app.get("/discover", tags=["read"])
    async def discover_endpoint() -> list[dict[str, Any]]:
        found: list[DiscoveredPrinter] = await lan_discover(timeout=2.0, retries=2)
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

    @app.get("/events/status", tags=["read"])
    async def status_stream(
        request: Request, manager: PrinterManager = Depends(get_manager)
    ) -> EventSourceResponse:
        """Server-Sent Events stream of live status pushes.

        Clients subscribe once and receive one ``data:`` line per push.
        Disconnects are handled silently on the server side.
        """

        async def gen() -> AsyncIterator[dict[str, str]]:
            try:
                async for st in manager.printer.watch():
                    if await request.is_disconnected():
                        break
                    yield {"event": "status", "data": json.dumps(st.raw, default=str)}
            except PrinterError as err:
                yield {"event": "error", "data": str(err)}

        return EventSourceResponse(gen())

    # --- Canvas -------------------------------------------------------------

    @app.get("/canvas", tags=["read"])
    async def canvas_status(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        """Canvas multi-filament system status (CC2 only)."""
        try:
            cs = await manager.printer.canvas_status()
        except RequestTimeoutError as err:
            # Transient (e.g. the CC2's rate-limit cooldown) — not a
            # capability gap. 504 so clients know to retry.
            raise HTTPException(status_code=504, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=501, detail=str(err)) from err
        return cs.raw

    @app.get("/files", tags=["read"])
    async def list_files(
        manager: PrinterManager = Depends(get_manager),
        storage: str = "local",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List files on the printer. Query: ?storage=local|u-disk&offset=0&limit=100."""
        try:
            return await manager.printer.list_files(storage, offset=offset, limit=limit)
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=501, detail=str(err)) from err

    @app.post("/files/delete", tags=["control"])
    async def delete_files(
        body: dict[str, Any],
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        """Delete file(s) from the printer. Body: {"filenames": [...], "storage": "local"}."""
        filenames = body.get("filenames", [])
        storage = body.get("storage", "local")
        if not filenames:
            raise HTTPException(status_code=400, detail="filenames list is required")
        if not isinstance(filenames, list) or not all(isinstance(f, str) for f in filenames):
            # Guard the SDCP channel: a bare string would iterate per-character
            # into a batch of malformed delete Cmds.
            raise HTTPException(status_code=400, detail="filenames must be a list of strings")
        try:
            result = await manager.printer.delete_files(filenames, storage=storage)
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "deleted": filenames, "response": result}

    @app.get("/disk", tags=["read"])
    async def disk_info(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        """Disk usage (CC2 only): total_bytes, used_bytes."""
        try:
            return await manager.printer.disk_info()
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=501, detail=str(err)) from err

    @app.get("/history", tags=["read"])
    async def print_history(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        """Print history."""
        try:
            return await manager.printer.print_history()
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=501, detail=str(err)) from err

    # --- RTSP bridge --------------------------------------------------------

    def _rtsp_state(request: Request) -> dict[str, Any]:
        controller: RtspController | None = getattr(request.app.state, "rtsp", None)
        if controller is None:
            return {
                "enabled": False,
                "available": False,
                "running": False,
                "urls": [],
                "advertised_urls": [],
                "reason": "RTSP feature not enabled on server; pass --rtsp on centauri server",
            }
        advertised_host = request.url.hostname
        return {
            "enabled": True,
            "available": controller.available(),
            "running": controller.running,
            "port": controller.cfg.rtsp_port,
            "path": controller.cfg.path,
            "fps": controller.cfg.fps,
            "bitrate": controller.cfg.bitrate,
            "urls": controller.urls(),
            "advertised_urls": controller.urls(advertised_host=advertised_host),
            "reason": controller.unavailable_reason() or controller.last_error,
        }

    @app.get("/api/rtsp", tags=["rtsp"])
    async def rtsp_status(request: Request) -> dict[str, Any]:
        return _rtsp_state(request)

    @app.post("/api/rtsp/start", tags=["rtsp"])
    async def rtsp_start(request: Request) -> dict[str, Any]:
        controller: RtspController | None = getattr(request.app.state, "rtsp", None)
        if controller is None:
            raise HTTPException(
                status_code=404,
                detail="RTSP feature not enabled. Launch server with --rtsp.",
            )
        try:
            await controller.start()
        except rtsp_module.RtspError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err
        return _rtsp_state(request)

    @app.post("/api/rtsp/stop", tags=["rtsp"])
    async def rtsp_stop(request: Request) -> dict[str, Any]:
        controller: RtspController | None = getattr(request.app.state, "rtsp", None)
        if controller is None:
            raise HTTPException(
                status_code=404,
                detail="RTSP feature not enabled. Launch server with --rtsp.",
            )
        await controller.stop()
        return _rtsp_state(request)

    # --- Meta / health ------------------------------------------------------

    @app.get("/api/info", tags=["meta"])
    async def api_info(
        manager: PrinterManager = Depends(get_manager),
    ) -> dict[str, Any]:
        update: _UpdateState | None = getattr(app.state, "update", None)
        latest = update.latest if update is not None else None
        return {
            "service": "pycentauri",
            "version": __version__,
            "latest_version": latest,
            "update_available": _update_available(__version__, latest),
            "printer_host": manager.host,
            "mainboard_id": manager._mainboard_id,
            "connected": manager._printer is not None and not manager._printer._closed,
            "enable_control": manager.enable_control,
        }

    # --- Static web UI ------------------------------------------------------

    class _NoCacheStatic(StaticFiles):
        # The dashboard updates with pycentauri; force browsers to revalidate
        # (cheap via ETag) so a redeploy is picked up on a normal reload
        # instead of serving a stale cached app.js.
        async def get_response(self, path: str, scope: Any) -> Response:
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    _web_root = resource_files("pycentauri").joinpath("web")
    if _web_root.is_dir():
        app.mount("/ui", _NoCacheStatic(directory=str(_web_root), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def root_redirect() -> RedirectResponse:
            return RedirectResponse(url="/ui/", status_code=307)
    else:

        @app.get("/", tags=["meta"])
        async def root_fallback(
            manager: PrinterManager = Depends(get_manager),
        ) -> dict[str, Any]:
            # Web UI assets not present — fall back to JSON health.
            return {
                "service": "pycentauri",
                "version": __version__,
                "printer_host": manager.host,
                "enable_control": manager.enable_control,
                "ui": "not installed",
            }

    if not enable_control:
        return app

    # --- Control endpoints (registered only when --enable-control) ----------

    def _sent_unconfirmed(verb: str, err: Exception) -> str:
        return (
            f"{verb} command was sent but the printer did not confirm in time "
            f"({err}). It may still complete — check the printer status."
        )

    @app.post("/print/start", tags=["control"])
    async def start_print(
        body: StartPrintBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.start_print(
                body.filename,
                storage=body.storage,
                auto_leveling=body.auto_leveling,
                timelapse=body.timelapse,
            )
        except ControlDisabledError as err:
            raise HTTPException(status_code=403, detail=str(err)) from err
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=_sent_unconfirmed("start", err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/pause", tags=["control"])
    async def pause_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.pause()
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=_sent_unconfirmed("pause", err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/resume", tags=["control"])
    async def resume_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.resume()
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=_sent_unconfirmed("resume", err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/stop", tags=["control"])
    async def stop_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.stop()
        except RequestTimeoutError as err:
            raise HTTPException(status_code=504, detail=_sent_unconfirmed("stop", err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/speed", tags=["control"])
    async def set_speed(
        body: PrintSpeedBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.set_print_speed(body.mode)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/fan", tags=["control"])
    async def set_fan(
        body: FanSpeedBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.set_fan_speed(
                model=body.model,
                auxiliary=body.auxiliary,
                chamber=body.chamber,
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/temperature", tags=["control"])
    async def set_temperature(
        body: TemperatureBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.set_temperatures(
                nozzle=body.nozzle, bed=body.bed, chamber=body.chamber
            )
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/canvas/refill", tags=["control"])
    async def set_refill(
        body: RefillBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.set_auto_refill(body.enabled)
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/light", tags=["control"])
    async def set_light(
        body: LightBody, manager: PrinterManager = Depends(require_control)
    ) -> dict[str, Any]:
        """Turn the chamber light on/off."""
        try:
            result = await manager.printer.set_light(body.on)
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/files/upload", tags=["control"])
    async def upload_file_route(
        file: UploadFile = File(..., description="The file to upload (e.g. a .gcode)."),
        start: bool = Form(False),
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        # Spool the browser upload to a temp file, then chunk it to the
        # printer over HTTP. Path(...).name strips any directory components
        # from the client-supplied filename (no traversal).
        remote_name = Path(file.filename or "upload.gcode").name
        fd, tmp_path = tempfile.mkstemp(suffix=f"_{remote_name}")
        try:
            with os.fdopen(fd, "wb") as tmp:
                while chunk := await file.read(1024 * 1024):
                    tmp.write(chunk)
            remote = await manager.printer.upload_file(tmp_path, remote_name=remote_name)
            resp: dict[str, Any] = {"ok": True, "filename": remote}
            if start:
                started = await manager.printer.start_print(remote)
                resp["start_response"] = started.inner
            return resp
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    return app


def run(
    host: str,
    *,
    bind: str = "127.0.0.1",
    port: int = 8787,
    enable_control: bool = False,
    mainboard_id: str | None = None,
    access_code: str | None = None,
    log_level: str = "info",
    rtsp_config: rtsp_module.RtspConfig | None = None,
    check_updates: bool = False,
) -> None:
    """Launch the server with uvicorn (blocks).

    Defaults bind to loopback. Set ``bind="0.0.0.0"`` to expose on the LAN —
    in that case put an authenticating reverse proxy in front, since the
    HTTP surface itself is unauthenticated.
    """
    import uvicorn

    # Surface pycentauri's own log lines (reconnects, speed-mode restores)
    # in the journal — uvicorn only configures its own loggers, and the
    # root logger's lastResort handler hides INFO.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")

    app = create_app(
        host,
        enable_control=enable_control,
        mainboard_id=mainboard_id,
        access_code=access_code,
        rtsp_config=rtsp_config,
        check_updates=check_updates,
    )
    # Bound graceful shutdown: open SSE/MJPEG streams never close on
    # their own, and without a timeout uvicorn waits for them forever —
    # leaving a zombie process that still holds a printer connection
    # (observed twice: 2026-06-23 and 2026-07-05).
    uvicorn.run(
        app,
        host=bind,
        port=port,
        log_level=log_level,
        timeout_graceful_shutdown=5,
    )


__all__ = ["JSONResponse", "PrinterManager", "create_app", "run"]
