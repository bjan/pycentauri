"""FastAPI HTTP server exposing the printer over REST + SSE.

A single long-lived :class:`~pycentauri.client.Printer` connection is held for
the lifetime of the server and reused across requests. That keeps us
well under the Elegoo firmware's 5-WebSocket slot limit and means
``GET /status`` returns the in-memory cached push rather than opening a
fresh connection on every request.

On WebSocket errors we reconnect in the background with exponential
backoff. The mainboard ID learned from the first successful discovery is
cached so reconnects don't have to wait on an Attributes push.

Surfaces (register with ``centauri server``):

* ``GET /`` — health and version info
* ``GET /status`` — latest status snapshot (JSON)
* ``GET /attributes`` — printer attributes (JSON)
* ``GET /snapshot`` — single JPEG frame from the webcam
* ``GET /discover`` — UDP LAN scan
* ``GET /events/status`` — Server-Sent Events stream of live status pushes
* ``POST /print/{start,pause,resume,stop}`` — only registered when the
  server is launched with ``--enable-control``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files as resource_files
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from pycentauri import __version__
from pycentauri import rtsp as rtsp_module
from pycentauri.camera import CAMERA_PATH, CAMERA_PORT
from pycentauri.client import ControlDisabledError, Printer, PrinterError
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
    ) -> None:
        self.host = host
        self.enable_control = enable_control
        self._mainboard_id = mainboard_id
        self._printer: Printer | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = False

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
                self._printer = await Printer.connect(
                    self.host,
                    enable_control=self.enable_control,
                    mainboard_id=self._mainboard_id,
                )
                # Prime the subscription so status pushes start flowing.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._printer._ensure_subscribed(),
                        timeout=5.0,
                    )
                log.info("connected to %s", self.host)
                self._ready.set()
                backoff = RECONNECT_BACKOFF_START
                # Hold until the reader dies (disconnect).
                reader = self._printer._reader
                if reader is not None:
                    await reader
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


def create_app(
    host: str,
    *,
    enable_control: bool = False,
    mainboard_id: str | None = None,
    rtsp_config: rtsp_module.RtspConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app. ``host`` is the printer's IP/hostname.

    The app runs a single background :class:`PrinterManager` that owns the
    WebSocket lifecycle. Control endpoints are registered only when
    ``enable_control`` is ``True``. ``rtsp_config`` enables the
    ``/api/rtsp/*`` endpoints and the "STREAM" panel in the web UI.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager = PrinterManager(host, enable_control=enable_control, mainboard_id=mainboard_id)
        app.state.manager = manager
        app.state.rtsp = RtspController(rtsp_config) if rtsp_config is not None else None
        await manager.start()
        try:
            yield
        finally:
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

        The printer serves ``multipart/x-mixed-replace`` on port 3031.
        Browsers can render this directly in an ``<img src=...>`` tag. We
        proxy it through the API server so the UI never needs to know the
        printer's IP and so cross-origin isn't an issue.
        """
        url = f"http://{manager.host}:{CAMERA_PORT}{CAMERA_PATH}"
        client = httpx.AsyncClient(timeout=None)

        async def body() -> AsyncIterator[bytes]:
            try:
                async with client.stream("GET", url) as upstream:
                    if upstream.status_code != 200:
                        return
                    async for chunk in upstream.aiter_raw():
                        yield chunk
            except Exception as err:
                log.warning("MJPEG proxy error: %r", err)
            finally:
                with contextlib.suppress(Exception):
                    await client.aclose()

        return StreamingResponse(
            body(),
            media_type="multipart/x-mixed-replace; boundary=--foo",
        )

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
        return {
            "service": "pycentauri",
            "version": __version__,
            "printer_host": manager.host,
            "mainboard_id": manager._mainboard_id,
            "connected": manager._printer is not None and not manager._printer._closed,
            "enable_control": manager.enable_control,
        }

    # --- Static web UI ------------------------------------------------------

    _web_root = resource_files("pycentauri").joinpath("web")
    if _web_root.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_web_root), html=True), name="ui")

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
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/pause", tags=["control"])
    async def pause_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.pause()
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/resume", tags=["control"])
    async def resume_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.resume()
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    @app.post("/print/stop", tags=["control"])
    async def stop_print(
        manager: PrinterManager = Depends(require_control),
    ) -> dict[str, Any]:
        try:
            result = await manager.printer.stop()
        except PrinterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {"ok": True, "response": result.inner}

    return app


def run(
    host: str,
    *,
    bind: str = "127.0.0.1",
    port: int = 8787,
    enable_control: bool = False,
    mainboard_id: str | None = None,
    log_level: str = "info",
    rtsp_config: rtsp_module.RtspConfig | None = None,
) -> None:
    """Launch the server with uvicorn (blocks).

    Defaults bind to loopback. Set ``bind="0.0.0.0"`` to expose on the LAN —
    in that case put an authenticating reverse proxy in front, since the
    HTTP surface itself is unauthenticated in v0.2.
    """
    import uvicorn

    app = create_app(
        host,
        enable_control=enable_control,
        mainboard_id=mainboard_id,
        rtsp_config=rtsp_config,
    )
    uvicorn.run(app, host=bind, port=port, log_level=log_level)


__all__ = ["JSONResponse", "PrinterManager", "create_app", "run"]
