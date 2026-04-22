"""Grab JPEG snapshots from the Centauri Carbon's built-in webcam.

The printer exposes an MJPEG stream at ``http://<host>:3031/video``
(``multipart/x-mixed-replace``). A snapshot is the first complete JPEG
frame (SOI ``FF D8`` through EOI ``FF D9``) we can read, after which we
close the connection.
"""

from __future__ import annotations

import httpx

CAMERA_PORT = 3031
CAMERA_PATH = "/video"
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
DEFAULT_TIMEOUT = 10.0
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MB safety cap


class SnapshotError(RuntimeError):
    """Raised when the webcam endpoint is unreachable or returns no frame."""


async def snapshot(
    host: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    port: int = CAMERA_PORT,
    path: str = CAMERA_PATH,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Return a single JPEG frame from the webcam as bytes.

    ``client`` can be supplied to reuse a configured AsyncClient (e.g. for
    tests against a mock server); otherwise we create one per call.
    """
    url = f"http://{host}:{port}{path}"
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        buf = bytearray()
        start: int | None = None
        async with client.stream("GET", url, timeout=timeout) as response:
            if response.status_code != 200:
                raise SnapshotError(f"webcam returned HTTP {response.status_code} from {url}")
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                buf.extend(chunk)
                if start is None:
                    i = buf.find(SOI)
                    if i >= 0:
                        start = i
                if start is not None:
                    j = buf.find(EOI, start + 2)
                    if j >= 0:
                        return bytes(buf[start : j + 2])
                if len(buf) > MAX_FRAME_BYTES:
                    raise SnapshotError("webcam frame exceeded size cap")
        raise SnapshotError("webcam stream ended before a complete JPEG arrived")
    finally:
        if owns_client:
            await client.aclose()
