"""High-level async client for the Elegoo Centauri Carbon.

Opens a single WebSocket to ``ws://<host>:3030/websocket``, routes responses
back to the requesting coroutines by ``RequestID``, and publishes status /
attribute pushes to any number of subscribers. Control actions are gated
behind ``enable_control=True`` — attempting a write without it raises
:class:`ControlDisabledError` before anything is sent over the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

import websockets
from typing_extensions import Self
from websockets.asyncio.client import ClientConnection, connect

from pycentauri import camera as camera_module
from pycentauri import sdcp
from pycentauri.models import Attributes, Status

log = logging.getLogger(__name__)


WS_PORT = 3030
WS_PATH = "/websocket"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_REQUEST_TIMEOUT = 15.0
DEFAULT_PUSH_PERIOD_MS = sdcp.DEFAULT_PUSH_PERIOD_MS


class PrinterError(RuntimeError):
    """Base for all client-side printer errors."""


class ControlDisabledError(PrinterError):
    """Raised when a control action is attempted without ``enable_control``."""


class RequestTimeoutError(PrinterError):
    """Raised when a request to the printer does not receive a response in time."""


class Printer:
    """Async client for a single Centauri Carbon.

    Usage::

        async with await Printer.connect("192.168.1.209") as printer:
            status = await printer.status()

    Or without the context manager::

        printer = await Printer.connect("192.168.1.209")
        try:
            ...
        finally:
            await printer.close()
    """

    def __init__(
        self,
        host: str,
        *,
        enable_control: bool = False,
        push_period_ms: int = DEFAULT_PUSH_PERIOD_MS,
    ) -> None:
        self.host = host
        self.enable_control = enable_control
        self.push_period_ms = push_period_ms

        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._mainboard_id: str | None = None

        self._mainboard_event = asyncio.Event()
        self._latest_status: Status | None = None
        self._latest_status_event = asyncio.Event()
        self._latest_attributes: Attributes | None = None
        self._latest_attributes_event = asyncio.Event()

        self._pending: dict[str, asyncio.Future[sdcp.ParsedMessage]] = {}
        self._status_queues: set[asyncio.Queue[Status]] = set()
        self._closed = False

    @classmethod
    async def connect(
        cls,
        host: str,
        *,
        enable_control: bool = False,
        push_period_ms: int = DEFAULT_PUSH_PERIOD_MS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> Self:
        """Open a WebSocket to the printer and start the reader task."""
        self = cls(host, enable_control=enable_control, push_period_ms=push_period_ms)
        url = f"ws://{host}:{WS_PORT}{WS_PATH}"
        self._ws = await asyncio.wait_for(connect(url, max_size=None), timeout=connect_timeout)
        self._reader = asyncio.create_task(self._read_loop(), name=f"pycentauri-reader-{host}")
        return self

    # --- context-manager sugar -------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # --- public high-level API -------------------------------------------------

    @property
    def mainboard_id(self) -> str | None:
        """Mainboard ID learned from the printer (set once the first Attributes push arrives)."""
        return self._mainboard_id

    async def wait_for_mainboard(self, timeout: float = 5.0) -> str:
        """Block until the printer has reported its mainboard ID."""
        if self._mainboard_id:
            return self._mainboard_id
        # The printer sends an Attributes push shortly after connect; a fresh
        # GET_PRINTER_ATTRIBUTES also triggers one. Fire one off just in case.
        with contextlib.suppress(Exception):
            await self._send_raw(
                sdcp.encode(
                    sdcp.build_request(
                        sdcp.Cmd.GET_PRINTER_ATTRIBUTES, None, self._mainboard_id or ""
                    )
                )
            )
        await asyncio.wait_for(self._mainboard_event.wait(), timeout=timeout)
        assert self._mainboard_id is not None
        return self._mainboard_id

    async def status(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Status:
        """Get the current printer status.

        If we've already received a push, return it immediately. Otherwise
        subscribe briefly (Cmd 512) and wait for the first push.
        """
        if self._latest_status is not None:
            return self._latest_status
        await self._ensure_subscribed()
        await asyncio.wait_for(self._latest_status_event.wait(), timeout=timeout)
        assert self._latest_status is not None
        return self._latest_status

    async def attributes(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Attributes:
        """Return the printer's attributes (model, firmware, capabilities)."""
        if self._latest_attributes is not None:
            return self._latest_attributes
        mid = await self.wait_for_mainboard(timeout=timeout)
        await self._request(sdcp.Cmd.GET_PRINTER_ATTRIBUTES, None, mid, timeout=timeout)
        await asyncio.wait_for(self._latest_attributes_event.wait(), timeout=timeout)
        assert self._latest_attributes is not None
        return self._latest_attributes

    async def watch(self) -> AsyncIterator[Status]:
        """Yield status updates as they arrive from the printer."""
        await self._ensure_subscribed()
        queue: asyncio.Queue[Status] = asyncio.Queue(maxsize=64)
        self._status_queues.add(queue)
        try:
            if self._latest_status is not None:
                queue.put_nowait(self._latest_status)
            while not self._closed:
                yield await queue.get()
        finally:
            self._status_queues.discard(queue)

    async def snapshot(self, *, timeout: float = camera_module.DEFAULT_TIMEOUT) -> bytes:
        """Return a single JPEG frame from the built-in webcam."""
        return await camera_module.snapshot(self.host, timeout=timeout)

    # --- control actions (gated) ----------------------------------------------

    async def start_print(
        self,
        filename: str,
        *,
        storage: str = "local",
        auto_leveling: bool = True,
        timelapse: bool = False,
    ) -> sdcp.ParsedMessage:
        """Start a print of an existing file on the printer.

        ``storage`` is either ``"local"`` (internal storage) or ``"udisk"``
        (USB). The filename is the name used by the printer, not a local path.
        """
        self._require_control("start_print")
        path_prefix = "/usb" if storage == "udisk" else "/local"
        data: dict[str, Any] = {
            "Filename": filename,
            "StartLayer": 0,
            "Calibration_switch": 1 if auto_leveling else 0,
            "PrintPlatformType": 0,
            "Tlp_Switch": 1 if timelapse else 0,
            "slot_map": [],
            "path_prefix": path_prefix,
        }
        mid = await self.wait_for_mainboard()
        return await self._request(sdcp.Cmd.START_PRINT, data, mid)

    async def pause(self) -> sdcp.ParsedMessage:
        self._require_control("pause")
        mid = await self.wait_for_mainboard()
        return await self._request(sdcp.Cmd.PAUSE_PRINT, None, mid)

    async def resume(self) -> sdcp.ParsedMessage:
        self._require_control("resume")
        mid = await self.wait_for_mainboard()
        return await self._request(sdcp.Cmd.RESUME_PRINT, None, mid)

    async def stop(self) -> sdcp.ParsedMessage:
        self._require_control("stop")
        mid = await self.wait_for_mainboard()
        return await self._request(sdcp.Cmd.STOP_PRINT, None, mid)

    # --- lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        """Close the WebSocket and stop the reader."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(PrinterError("connection closed"))

    # --- internals -------------------------------------------------------------

    def _require_control(self, action: str) -> None:
        if not self.enable_control:
            raise ControlDisabledError(
                f"{action!r} requires enable_control=True; "
                "Printer.connect(..., enable_control=True) to allow write actions"
            )

    async def _send_raw(self, text: str) -> None:
        if self._ws is None:
            raise PrinterError("not connected")
        await self._ws.send(text)

    async def _ensure_subscribed(self) -> None:
        mid = await self.wait_for_mainboard()
        pkt = sdcp.build_subscribe(mid, period_ms=self.push_period_ms)
        await self._send_raw(sdcp.encode(pkt))

    async def _request(
        self,
        cmd: int,
        data: dict[str, Any] | None,
        mainboard_id: str,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> sdcp.ParsedMessage:
        pkt = sdcp.build_request(cmd, data, mainboard_id)
        request_id = pkt["Data"]["RequestID"]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[sdcp.ParsedMessage] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self._send_raw(sdcp.encode(pkt))
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as err:
            raise RequestTimeoutError(f"cmd {cmd} timed out after {timeout}s") from err
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    self._handle_frame(raw)
                except Exception:
                    log.exception("failed to handle frame: %r", raw[:200] if raw else raw)
        except websockets.ConnectionClosed:
            log.debug("ws closed by peer")
        except Exception:
            log.exception("ws reader crashed")
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PrinterError("connection closed"))

    def _handle_frame(self, raw: str | bytes) -> None:
        msg = sdcp.parse_message(raw)

        if msg.mainboard_id and self._mainboard_id is None:
            self._mainboard_id = msg.mainboard_id
            self._mainboard_event.set()
            log.debug("learned mainboard id: %s", msg.mainboard_id)

        if msg.type == sdcp.MessageType.STATUS and msg.status is not None:
            status = Status.from_payload(msg.status)
            self._latest_status = status
            self._latest_status_event.set()
            for q in list(self._status_queues):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(status)

        elif msg.type == sdcp.MessageType.ATTRIBUTES and msg.attributes is not None:
            self._latest_attributes = Attributes.from_payload(msg.attributes)
            self._latest_attributes_event.set()

        if msg.request_id and msg.request_id in self._pending:
            fut = self._pending[msg.request_id]
            if not fut.done():
                fut.set_result(msg)
