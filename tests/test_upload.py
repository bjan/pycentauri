"""Unit tests for the chunked HTTP file-upload layer.

No printer: a recording fake ``httpx.AsyncClient`` captures every chunk so
we can assert offsets, framing, MD5, and success/failure handling for both
the CC1 (multipart POST) and CC2 (Content-Range PUT) protocols.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from pycentauri import upload
from pycentauri.client import PrinterError


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _RecordingClient:
    """Records every POST/PUT; returns a canned response per call."""

    posts: ClassVar[list[dict[str, Any]]] = []
    puts: ClassVar[list[dict[str, Any]]] = []
    post_response: ClassVar[_FakeResponse] = _FakeResponse(200, '{"code": "000000"}')
    put_response: ClassVar[_FakeResponse] = _FakeResponse(200, '{"error_code": 0}')

    def __init__(self, *a: Any, **k: Any) -> None: ...
    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def post(self, url: str, *, data: Any = None, files: Any = None) -> _FakeResponse:
        type(self).posts.append({"url": url, "data": data, "files": files})
        return type(self).post_response

    async def put(self, url: str, *, content: Any = None, headers: Any = None) -> _FakeResponse:
        type(self).puts.append({"url": url, "content": content, "headers": headers})
        return type(self).put_response


@pytest.fixture(autouse=True)
def _reset() -> None:
    _RecordingClient.posts = []
    _RecordingClient.puts = []
    _RecordingClient.post_response = _FakeResponse(200, '{"code": "000000"}')
    _RecordingClient.put_response = _FakeResponse(200, '{"error_code": 0}')


def _make_file(tmp_path: Path, size: int) -> Path:
    # Deterministic, non-trivial content spanning multiple 1 MiB chunks.
    data = bytes((i * 7 + 3) % 256 for i in range(size))
    p = tmp_path / "model.gcode"
    p.write_bytes(data)
    return p


async def test_cc1_chunks_offsets_and_md5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    size = upload.CHUNK_SIZE * 2 + 12345  # 3 chunks: 1M, 1M, remainder
    path = _make_file(tmp_path, size)
    expected_md5 = hashlib.md5(path.read_bytes()).hexdigest()

    name = await upload.upload_cc1("1.2.3.4", path)
    assert name == "model.gcode"

    posts = _RecordingClient.posts
    assert len(posts) == 3
    assert posts[0]["url"] == "http://1.2.3.4:80/uploadFile/upload"
    # offsets advance by chunk size, cover the whole file exactly
    offsets = [int(p["data"]["Offset"]) for p in posts]
    assert offsets == [0, upload.CHUNK_SIZE, upload.CHUNK_SIZE * 2]
    # every chunk carries the whole-file md5 + total size + one shared uuid
    assert {p["data"]["S-File-MD5"] for p in posts} == {expected_md5}
    assert {p["data"]["TotalSize"] for p in posts} == {str(size)}
    assert len({p["data"]["Uuid"] for p in posts}) == 1
    # the chunk bodies reassemble to the original file
    body = b"".join(p["files"]["File"][1] for p in posts)
    assert body == path.read_bytes()


async def test_cc1_progress_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    size = upload.CHUNK_SIZE + 500
    path = _make_file(tmp_path, size)
    seen: list[tuple[int, int]] = []
    await upload.upload_cc1("1.2.3.4", path, progress=lambda s, t: seen.append((s, t)))
    assert seen[-1] == (size, size)  # ends at 100%
    assert all(t == size for _, t in seen)


async def test_cc1_rejected_chunk_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.post_response = _FakeResponse(200, '{"code": "999999"}')
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    path = _make_file(tmp_path, 1000)
    with pytest.raises(PrinterError, match="rejected"):
        await upload.upload_cc1("1.2.3.4", path)


async def test_cc2_content_range_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    size = upload.CHUNK_SIZE + 100  # 2 chunks
    path = _make_file(tmp_path, size)
    expected_md5 = hashlib.md5(path.read_bytes()).hexdigest()

    await upload.upload_cc2("5.6.7.8", path, access_code="SECRET", remote_name="job.gcode")

    puts = _RecordingClient.puts
    assert len(puts) == 2
    assert puts[0]["url"] == "http://5.6.7.8:80/upload"
    assert puts[0]["headers"]["Content-Range"] == f"bytes 0-{upload.CHUNK_SIZE - 1}/{size}"
    assert puts[1]["headers"]["Content-Range"] == f"bytes {upload.CHUNK_SIZE}-{size - 1}/{size}"
    assert {p["headers"]["X-File-MD5"] for p in puts} == {expected_md5}
    assert {p["headers"]["X-File-Name"] for p in puts} == {"job.gcode"}
    assert {p["headers"]["X-Token"] for p in puts} == {"SECRET"}
    assert b"".join(p["content"] for p in puts) == path.read_bytes()


async def test_cc2_401_names_access_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingClient.put_response = _FakeResponse(401, "")
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingClient)
    path = _make_file(tmp_path, 1000)
    with pytest.raises(PrinterError, match="access code"):
        await upload.upload_cc2("5.6.7.8", path, access_code="wrong")


async def test_empty_file_refused(tmp_path: Path) -> None:
    p = tmp_path / "empty.gcode"
    p.write_bytes(b"")
    with pytest.raises(PrinterError, match="empty"):
        await upload.upload_cc1("1.2.3.4", p)


async def test_missing_file_refused() -> None:
    with pytest.raises(PrinterError, match="not a file"):
        await upload.upload_cc1("1.2.3.4", "/nonexistent/path.gcode")
