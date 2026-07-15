"""One shared upstream MJPEG connection, fanned out to many subscribers.

The printer's camera server has very few connection slots and releases them
badly — a closed connection lingers (``FIN-WAIT-2``: the printer never sends
its FIN) and keeps holding a slot. Opening a fresh upstream per browser
request — which the old ``/stream`` did, once per tab *and* once per
client-side reload — exhausts those slots and starves the stream. Observed
live 2026-07-08 on CC1 mid-print: a fresh sole-client connection got zero
frames because stale slots were still held.

:class:`CameraBroadcaster` holds a *single* upstream connection for the
server's lifetime and broadcasts its raw bytes to every subscriber, so the
printer only ever sees one camera connection no matter how many browsers,
tabs, or reloads happen. Slow subscribers get old frames dropped rather than
stalling the reader or their peers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_MEDIA_TYPE = "multipart/x-mixed-replace; boundary=frame"
#: Per-subscriber buffer. Dropping old frames past this keeps a slow browser
#: from backing up the shared reader.
QUEUE_MAX = 64
#: How long ``subscribe()`` waits for the first upstream connect before 502.
CONNECT_WAIT_S = 8.0
#: If no frame arrives from the upstream for this long, consider it stale and
#: reconnect. The printer's camera can silently stop sending while the TCP
#: socket stays open (observed 2026-07-09 on CC1: ESTAB connection, zero
#: frames until service restart).
STALE_TIMEOUT_S = 15.0
_BACKOFF_START = 1.0
_BACKOFF_MAX = 10.0


class CameraUnavailable(RuntimeError):
    """The upstream camera could not be reached in time."""


@dataclass
class _Upstream:
    media_type: str
    chunks: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


class CameraBroadcaster:
    def __init__(
        self,
        url_factory: Callable[[], str],
        *,
        open_source: Callable[[], Awaitable[_Upstream]] | None = None,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self._url_factory = url_factory
        self._open_source = open_source or self._httpx_open
        self._queue_max = queue_max
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._media_type = DEFAULT_MEDIA_TYPE
        self._connected = asyncio.Event()
        self._closing = False

    async def subscribe(self) -> tuple[str, AsyncIterator[bytes]]:
        """Register a subscriber; returns ``(media_type, chunk iterator)``.

        Starts the shared reader on the first subscriber. Raises
        :class:`CameraUnavailable` if the upstream never connects.
        """
        if self._closing:
            raise CameraUnavailable("broadcaster is shutting down")
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.add(queue)
        if self._reader is None or self._reader.done():
            self._connected.clear()
            self._reader = asyncio.create_task(self._reader_loop(), name="pycentauri-mjpeg")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=CONNECT_WAIT_S)
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._subscribers.discard(queue)
            raise CameraUnavailable("camera did not start streaming in time") from err
        return self._media_type, self._drain(queue)

    async def _drain(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:  # shutdown sentinel
                    return
                yield chunk
        finally:
            self._subscribers.discard(queue)

    def _fanout(self, chunk: bytes) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                # Slow consumer: drop the oldest frame, keep the newest.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(chunk)

    async def _reader_loop(self) -> None:
        backoff = _BACKOFF_START
        while not self._closing:
            try:
                upstream = await self._open_source()
            except Exception as err:
                log.warning("camera upstream connect failed: %r", err)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
                continue
            self._media_type = upstream.media_type
            self._connected.set()
            backoff = _BACKOFF_START
            try:
                it = upstream.chunks.__aiter__()
                while not self._closing:
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=STALE_TIMEOUT_S)
                    except (TimeoutError, asyncio.TimeoutError):
                        log.warning(
                            "camera upstream stale (%ds no frame), reconnecting", STALE_TIMEOUT_S
                        )
                        break
                    except StopAsyncIteration:
                        break
                    self._fanout(chunk)
            except Exception as err:
                log.warning("camera upstream read ended: %r", err)
            finally:
                await upstream.aclose()
            # Upstream dropped or stale; reconnect (unless we're shutting down).
            self._connected.clear()

    async def _httpx_open(self) -> _Upstream:
        url = self._url_factory()
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)
        )
        cm = client.stream("GET", url)
        resp = await cm.__aenter__()
        if resp.status_code != 200:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            await client.aclose()
            raise CameraUnavailable(f"camera returned HTTP {resp.status_code}")
        media_type = resp.headers.get("content-type", DEFAULT_MEDIA_TYPE)

        async def aclose() -> None:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            with contextlib.suppress(Exception):
                await client.aclose()

        return _Upstream(media_type, resp.aiter_raw(), aclose)

    async def close(self) -> None:
        self._closing = True
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        # Release every subscriber's generator.
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self._subscribers.clear()
