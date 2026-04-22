"""Unit tests for the RTSP launcher — no processes actually spawned."""

from __future__ import annotations

from pathlib import Path

import pytest

from pycentauri.rtsp import (
    FFMPEG_HINT,
    INSTALL_HINT,
    RtspConfig,
    RtspError,
    build_urls,
    ensure_binaries,
    find_binary,
    render_config,
)


def test_find_binary_rejects_missing_override(tmp_path: Path) -> None:
    assert find_binary("mediamtx", override=str(tmp_path / "does-not-exist")) is None


def test_find_binary_accepts_executable_override(tmp_path: Path) -> None:
    fake = tmp_path / "fake-bin"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    assert find_binary("whatever", override=str(fake)) == str(fake)


def test_ensure_binaries_raises_when_mediamtx_missing(tmp_path: Path) -> None:
    cfg = RtspConfig(
        printer_host="192.168.1.209",
        mediamtx_path=str(tmp_path / "nope"),
    )
    with pytest.raises(RtspError) as excinfo:
        ensure_binaries(cfg)
    assert INSTALL_HINT in str(excinfo.value)


def test_ensure_binaries_raises_when_ffmpeg_missing(tmp_path: Path) -> None:
    mtx = tmp_path / "mediamtx"
    mtx.write_text("#!/bin/sh\nexit 0\n")
    mtx.chmod(0o755)
    cfg = RtspConfig(
        printer_host="192.168.1.209",
        mediamtx_path=str(mtx),
        ffmpeg_path=str(tmp_path / "no-ffmpeg"),
    )
    with pytest.raises(RtspError) as excinfo:
        ensure_binaries(cfg)
    assert FFMPEG_HINT in str(excinfo.value)


def test_render_config_contains_expected_fields() -> None:
    cfg = RtspConfig(
        printer_host="192.168.1.209",
        rtsp_port=8554,
        bind="0.0.0.0",
        path="printer",
        fps=15,
        bitrate="2M",
    )
    yaml = render_config(cfg, ffmpeg_bin="/usr/bin/ffmpeg")
    assert "rtspAddress: :8554" in yaml
    assert "http://192.168.1.209:3031/video" in yaml
    assert "paths:" in yaml
    assert "  printer:" in yaml
    assert "runOnDemand:" in yaml
    assert "/usr/bin/ffmpeg" in yaml
    assert "libx264" in yaml
    assert "-b:v 2M" in yaml
    assert "-r 15" in yaml
    assert "webrtc: no" in yaml
    assert "hls: no" in yaml


def test_render_config_explicit_bind() -> None:
    cfg = RtspConfig(printer_host="h", bind="127.0.0.1", rtsp_port=9000)
    yaml = render_config(cfg, ffmpeg_bin="ffmpeg")
    assert "rtspAddress: 127.0.0.1:9000" in yaml


def test_render_config_custom_path() -> None:
    cfg = RtspConfig(printer_host="h", path="workshop-cam-1")
    yaml = render_config(cfg, ffmpeg_bin="ffmpeg")
    assert "  workshop-cam-1:" in yaml


def test_render_config_enables_webrtc_and_hls_when_requested() -> None:
    cfg = RtspConfig(printer_host="h", enable_webrtc=True, enable_hls=True)
    yaml = render_config(cfg, ffmpeg_bin="ffmpeg")
    assert "webrtc: no" not in yaml
    assert "hls: no" not in yaml


def test_build_urls_masks_wildcard_bind() -> None:
    cfg = RtspConfig(printer_host="h", bind="0.0.0.0", rtsp_port=8554, path="printer")
    urls = build_urls(cfg)
    assert urls == ["rtsp://<this-host>:8554/printer"]


def test_build_urls_uses_advertised_host() -> None:
    cfg = RtspConfig(printer_host="h", bind="0.0.0.0", rtsp_port=8554, path="printer")
    assert build_urls(cfg, advertised_host="nix.brancloud.online") == [
        "rtsp://nix.brancloud.online:8554/printer"
    ]


def test_build_urls_with_explicit_bind() -> None:
    cfg = RtspConfig(printer_host="h", bind="192.168.1.101", rtsp_port=8554, path="cam")
    assert build_urls(cfg) == ["rtsp://192.168.1.101:8554/cam"]
