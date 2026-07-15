"""Tests for the shared-upstream MJPEG broadcaster.

A fake source stands in for the printer's camera so we can prove the
core promise: no matter how many subscribers attach, the upstream is
opened exactly once, and every subscriber sees the same frames.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from pycentauri.mjpeg_broadcast import CameraBroadcaster, CameraUnavailable, _Upstream


class _FakeSource:
    """A controllable upstream: opens counted, yields chunks on demand."""

    def __init__(self) -> None:
        self.opens = 0
        self.closed = 0
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def open(self) -> _Upstream:
        self.opens += 1

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item

        async def aclose() -> None:
            self.closed += 1

        return _Upstream("multipart/x-mixed-replace; boundary=xyz", chunks(), aclose)

    def push(self, data: bytes) -> None:
        self._queue.put_nowait(data)


async def _take(gen: AsyncIterator[bytes], n: int) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in gen:
        out.append(chunk)
        if len(out) >= n:
            break
    return out


async def test_single_upstream_for_many_subscribers() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)

    mt1, g1 = await b.subscribe()
    mt2, g2 = await b.subscribe()
    mt3, g3 = await b.subscribe()
    assert mt1 == mt2 == mt3 == "multipart/x-mixed-replace; boundary=xyz"
    # THE POINT: three subscribers, one upstream connection.
    assert src.opens == 1

    src.push(b"frameA")
    src.push(b"frameB")
    # Every subscriber sees the same broadcast frames.
    for g in (g1, g2, g3):
        got = await asyncio.wait_for(_take(g, 2), timeout=2.0)
        assert got == [b"frameA", b"frameB"]

    await b.close()
    assert src.closed >= 1


async def test_subscribe_times_out_when_upstream_never_connects() -> None:
    async def never_opens() -> _Upstream:
        raise RuntimeError("camera down")

    b = CameraBroadcaster(lambda: "http://x/", open_source=never_opens)
    import pycentauri.mjpeg_broadcast as m

    monkeyed = m.CONNECT_WAIT_S
    m.CONNECT_WAIT_S = 0.3
    try:
        with pytest.raises(CameraUnavailable):
            await b.subscribe()
    finally:
        m.CONNECT_WAIT_S = monkeyed
        await b.close()


async def test_slow_subscriber_does_not_stall_others() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open, queue_max=4)
    _mt_slow, _g_slow = await b.subscribe()  # never drained → queue overflows
    _mt_fast, g_fast = await b.subscribe()

    for i in range(20):
        src.push(f"f{i}".encode())
    # The fast subscriber still receives recent frames despite the slow one
    # never draining (old frames get dropped for the slow queue, not blocked).
    got = await asyncio.wait_for(_take(g_fast, 4), timeout=2.0)
    assert len(got) == 4
    assert src.opens == 1
    await b.close()


async def test_close_ends_subscriber_generators() -> None:
    src = _FakeSource()
    b = CameraBroadcaster(lambda: "http://x/", open_source=src.open)
    _mt, gen = await b.subscribe()
    await b.close()
    # After close, the generator finishes cleanly (no hang).
    remaining = [chunk async for chunk in gen]
    assert isinstance(remaining, list)


async def test_reader_reconnects_after_upstream_drop() -> None:
    opens = 0

    async def flaky_open() -> _Upstream:
        nonlocal opens
        opens += 1
        first = opens == 1

        async def chunks() -> AsyncIterator[bytes]:
            if first:
                yield b"before-drop"
                return  # upstream ends → reader should reconnect
            while True:
                await asyncio.sleep(0.01)
                yield b"after-reconnect"

        async def aclose() -> None: ...

        return _Upstream("mt", chunks(), aclose)

    b = CameraBroadcaster(lambda: "http://x/", open_source=flaky_open)
    _mt, gen = await b.subscribe()
    got = await asyncio.wait_for(_take(gen, 2), timeout=2.0)
    assert b"before-drop" in got or b"after-reconnect" in got
    assert opens >= 2  # reconnected after the first upstream ended
    await b.close()


async def test_stale_upstream_triggers_reconnect() -> None:
    """If the upstream stops sending frames (socket alive, no data), the
    reader must reconnect after STALE_TIMEOUT_S instead of hanging."""
    import pycentauri.mjpeg_broadcast as m

    opens = 0

    async def stalling_open() -> _Upstream:
        nonlocal opens
        opens += 1

        async def chunks() -> AsyncIterator[bytes]:
            yield b"one-frame"
            # Then go silent forever — simulating the observed stall.
            await asyncio.sleep(9999)

        async def aclose() -> None: ...

        return _Upstream("mt", chunks(), aclose)

    saved = m.STALE_TIMEOUT_S
    m.STALE_TIMEOUT_S = 0.3  # speed up the test
    try:
        b = CameraBroadcaster(lambda: "http://x/", open_source=stalling_open)
        _mt, gen = await b.subscribe()
        # The first open delivers one frame, then stalls; the reader should
        # time out and reconnect, giving us a second "one-frame".
        got = await asyncio.wait_for(_take(gen, 2), timeout=3.0)
        assert len(got) == 2
        assert opens >= 2
    finally:
        m.STALE_TIMEOUT_S = saved
        await b.close()
