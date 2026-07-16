"""Chunked HTTP file upload to Centauri printers.

File transfer runs over plain HTTP, entirely separate from the fragile
SDCP/MQTT control channel — so it can never crash a print. The two models
differ:

* **CC1** — ``POST /uploadFile/upload`` as multipart form-data, one 1 MiB
  chunk per request, fields ``Check``/``S-File-MD5``/``Offset``/``Uuid``/
  ``TotalSize``/``File``. Per-chunk success is JSON ``{"code": "000000"}``.
* **CC2** — ``PUT /upload`` with the raw chunk as the octet-stream body and
  ``Content-Range``/``X-File-Name``/``X-File-MD5``/``X-Token`` headers.
  Success is JSON ``error_code == 0``.

Both send the whole-file MD5 with every chunk and go to internal storage.
Endpoints and framing reverse-engineered from Elegoo's ``elegoo-link`` SDK
(``src/lan/adapters/elegoo_fdm_cc*``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid as uuid_mod
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from pycentauri.client import PrinterError

UPLOAD_PORT = 80
CHUNK_SIZE = 1024 * 1024  # 1 MiB — Elegoo's documented per-chunk maximum.
DEFAULT_TIMEOUT = 180.0
CC2_DEFAULT_TOKEN = "123456"

#: Called after each chunk with ``(bytes_sent, total_bytes)``.
ProgressCallback = Callable[[int, int], None]


def _file_md5(path: Path) -> str:
    h = hashlib.md5()  # Elegoo's protocol mandates MD5; not a security use.
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(block)
    return h.hexdigest()


async def _aiter_chunks(path: Path) -> AsyncIterator[tuple[int, bytes]]:
    """Yield ``(offset, chunk_bytes)`` over the file, 1 MiB at a time.

    Each read is pushed to a thread so a large file doesn't block the event
    loop (the server shares it with the SSE status stream and camera).
    """
    offset = 0
    with path.open("rb") as f:
        while True:
            chunk = await asyncio.to_thread(f.read, CHUNK_SIZE)
            if not chunk:
                return
            yield offset, chunk
            offset += len(chunk)


def _resolve(local_path: str | Path, remote_name: str | None) -> tuple[Path, int, str]:
    path = Path(local_path)
    if not path.is_file():
        raise PrinterError(f"not a file: {path}")
    total = path.stat().st_size
    if total == 0:
        raise PrinterError(f"refusing to upload an empty file: {path}")
    return path, total, (remote_name or path.name)


async def upload_cc1(
    host: str,
    local_path: str | Path,
    *,
    remote_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    progress: ProgressCallback | None = None,
) -> str:
    """Upload a file to a CC1 (multipart POST). Returns the remote filename."""
    path, total, name = _resolve(local_path, remote_name)
    md5 = await asyncio.to_thread(_file_md5, path)
    upload_id = uuid_mod.uuid4().hex
    url = f"http://{host}:{UPLOAD_PORT}/uploadFile/upload"
    async with httpx.AsyncClient(timeout=timeout) as client:
        async for offset, chunk in _aiter_chunks(path):
            resp = await client.post(
                url,
                data={
                    "Check": "1",
                    "S-File-MD5": md5,
                    "Offset": str(offset),
                    "Uuid": upload_id,
                    "TotalSize": str(total),
                },
                files={"File": (name, chunk, "application/octet-stream")},
            )
            _check_cc1_response(resp, offset)
            if progress is not None:
                progress(offset + len(chunk), total)
    return name


async def upload_cc2(
    host: str,
    local_path: str | Path,
    *,
    access_code: str,
    remote_name: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    progress: ProgressCallback | None = None,
) -> str:
    """Upload a file to a CC2 (PUT with Content-Range). Returns the remote filename."""
    path, total, name = _resolve(local_path, remote_name)
    md5 = await asyncio.to_thread(_file_md5, path)
    token = access_code or CC2_DEFAULT_TOKEN
    url = f"http://{host}:{UPLOAD_PORT}/upload"
    async with httpx.AsyncClient(timeout=timeout) as client:
        async for offset, chunk in _aiter_chunks(path):
            end = offset + len(chunk) - 1
            resp = await client.put(
                url,
                content=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "X-File-Name": name,
                    "X-File-MD5": md5,
                    "X-Token": token,
                },
            )
            _check_cc2_response(resp, offset)
            if progress is not None:
                progress(offset + len(chunk), total)
    return name


def _check_cc1_response(resp: httpx.Response, offset: int) -> None:
    if resp.status_code != 200:
        raise PrinterError(f"CC1 upload chunk at offset {offset} failed: HTTP {resp.status_code}")
    try:
        body = json.loads(resp.text)
    except (json.JSONDecodeError, ValueError) as err:
        raise PrinterError(
            f"CC1 upload chunk at offset {offset}: unparseable response {resp.text[:200]!r}"
        ) from err
    if body.get("code") != "000000":
        raise PrinterError(f"CC1 upload chunk at offset {offset} rejected: {body}")


def _check_cc2_response(resp: httpx.Response, offset: int) -> None:
    if resp.status_code == 401:
        raise PrinterError(
            "CC2 upload rejected the access code (HTTP 401). Check the code on the printer screen."
        )
    if resp.status_code not in (200, 201, 204):
        raise PrinterError(f"CC2 upload chunk at offset {offset} failed: HTTP {resp.status_code}")
    # A body is optional (some chunks 204); when present, a nonzero
    # error_code is a rejection.
    text = resp.text.strip()
    if not text:
        return
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(body, dict) and body.get("error_code") not in (0, None):
        raise PrinterError(f"CC2 upload chunk at offset {offset} rejected: {body}")
