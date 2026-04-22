"""Expose the printer's MJPEG webcam as an RTSP stream.

The Centauri Carbon serves MJPEG on ``http://<host>:3031/video``. Most
consumer video clients (VLC, Home Assistant, Jellyfin, Frigate, NVRs,
Synology Surveillance, most Android Shield/STB camera apps) prefer
RTSP/H.264. This module launches `MediaMTX`_ as a subprocess with an
on-demand transcode pipeline::

    printer MJPEG  →  ffmpeg (MJPEG → H.264)  →  MediaMTX (RTSP server)

MediaMTX only spawns the ffmpeg process while at least one client is
connected to the RTSP path, so idle cost is zero.

External binaries required at runtime:

* ``mediamtx``  — https://github.com/bluenviron/mediamtx/releases
* ``ffmpeg``    — any recent build on ``$PATH``

.. _MediaMTX: https://github.com/bluenviron/mediamtx
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class RtspError(RuntimeError):
    """Raised for setup failures — missing binaries, etc."""


@dataclass(slots=True)
class RtspConfig:
    """Parameters for the RTSP bridge."""

    printer_host: str
    rtsp_port: int = 8554
    bind: str = "0.0.0.0"
    path: str = "printer"
    camera_port: int = 3031
    camera_path: str = "/video"
    # ffmpeg encode tuning. ``veryfast`` + ``zerolatency`` keeps CPU low
    # and glass-to-glass latency around 500 ms to 1 s on modest hardware.
    preset: str = "veryfast"
    tune: str = "zerolatency"
    bitrate: str = "2M"
    fps: int = 15
    # Override binary paths if they're not on ``$PATH``.
    mediamtx_path: str | None = None
    ffmpeg_path: str | None = None
    # If set, also serve WebRTC / HLS on default ports; otherwise RTSP-only.
    enable_webrtc: bool = False
    enable_hls: bool = False
    log_level: str = "info"


INSTALL_HINT = (
    "MediaMTX is required and was not found on $PATH.\n"
    "Install it:\n"
    "  macOS:   brew install mediamtx\n"
    "  Linux:   download the latest linux_amd64 binary from\n"
    "           https://github.com/bluenviron/mediamtx/releases\n"
    "           and move it to /usr/local/bin/mediamtx (chmod +x)\n"
    "  Or pass --mediamtx-path /full/path/to/mediamtx."
)

FFMPEG_HINT = (
    "ffmpeg is required and was not found on $PATH.\n"
    "  macOS:   brew install ffmpeg\n"
    "  Linux:   sudo apt install ffmpeg (or your distro's equivalent)\n"
    "  Or pass --ffmpeg-path /full/path/to/ffmpeg."
)


def find_binary(name: str, override: str | None = None) -> str | None:
    """Locate a binary by name, respecting an explicit override."""
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        return None
    return shutil.which(name)


def _mediamtx_yaml(cfg: RtspConfig, ffmpeg_bin: str) -> str:
    """Render a MediaMTX YAML config for the requested parameters.

    ``runOnDemand`` keeps the ffmpeg process idle until the first RTSP
    reader connects; ``runOnDemandRestart`` restarts it if it exits while
    readers are still attached (printer reboots, network blips).
    """
    cam_url = f"http://{cfg.printer_host}:{cfg.camera_port}{cfg.camera_path}"
    rtsp_address = f"{cfg.bind}:{cfg.rtsp_port}" if cfg.bind != "0.0.0.0" else f":{cfg.rtsp_port}"
    webrtc_line = "" if cfg.enable_webrtc else "webrtc: no\n"
    hls_line = "" if cfg.enable_hls else "hls: no\n"

    # ffmpeg: take the MJPEG multipart stream, re-encode to H.264 baseline,
    # cap fps, push it to MediaMTX on localhost as a relay source. MediaMTX
    # substitutes $RTSP_PORT and $MTX_PATH when it executes this command.
    ffmpeg_cmd = (
        f"{ffmpeg_bin} -hide_banner -loglevel warning "
        f"-fflags nobuffer -flags low_delay "
        f"-f mjpeg -use_wallclock_as_timestamps 1 -i {cam_url} "
        f"-r {cfg.fps} "
        f"-c:v libx264 -preset {cfg.preset} -tune {cfg.tune} "
        f"-profile:v baseline -pix_fmt yuv420p -g {cfg.fps * 2} "
        f"-b:v {cfg.bitrate} -maxrate {cfg.bitrate} -bufsize {cfg.bitrate} "
        f"-an -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH"
    )

    return (
        f"# pycentauri-generated MediaMTX config — safe to delete.\n"
        f"logLevel: {cfg.log_level}\n"
        f"logDestinations: [stdout]\n"
        f"rtspAddress: {rtsp_address}\n"
        f"rtmp: no\n"
        f"{hls_line}"
        f"{webrtc_line}"
        f"srt: no\n"
        f"paths:\n"
        f"  {cfg.path}:\n"
        f"    runOnDemand: {ffmpeg_cmd}\n"
        f"    runOnDemandRestart: yes\n"
        f"    runOnDemandCloseAfter: 30s\n"
    )


def render_config(cfg: RtspConfig, ffmpeg_bin: str | None = None) -> str:
    """Public entry point for tests — render the YAML config as a string."""
    ffmpeg = ffmpeg_bin or find_binary("ffmpeg", cfg.ffmpeg_path) or "ffmpeg"
    return _mediamtx_yaml(cfg, ffmpeg)


def ensure_binaries(cfg: RtspConfig) -> tuple[str, str]:
    """Return ``(mediamtx_bin, ffmpeg_bin)`` or raise :class:`RtspError`."""
    mediamtx = find_binary("mediamtx", cfg.mediamtx_path)
    if mediamtx is None:
        raise RtspError(INSTALL_HINT)
    ffmpeg = find_binary("ffmpeg", cfg.ffmpeg_path)
    if ffmpeg is None:
        raise RtspError(FFMPEG_HINT)
    return mediamtx, ffmpeg


def build_urls(cfg: RtspConfig, *, advertised_host: str | None = None) -> list[str]:
    """Return the RTSP URLs a client should use to connect."""
    host = advertised_host or cfg.bind
    if host in ("0.0.0.0", "::", ""):
        host = "<this-host>"
    return [f"rtsp://{host}:{cfg.rtsp_port}/{cfg.path}"]


def run(cfg: RtspConfig) -> int:
    """Launch MediaMTX in the foreground. Returns its exit code.

    The caller owns the lifecycle — we block until MediaMTX exits or we get
    SIGINT/SIGTERM, then propagate the signal into the child and wait for
    it to clean up.
    """
    mediamtx_bin, ffmpeg_bin = ensure_binaries(cfg)

    yaml_text = _mediamtx_yaml(cfg, ffmpeg_bin)

    # Write the config to a tempfile that outlives this scope only until
    # MediaMTX has read it (which happens immediately at startup).
    fd, cfg_path = tempfile.mkstemp(prefix="pycentauri-mediamtx-", suffix=".yml", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml_text)

        print(f"pycentauri: launching MediaMTX ({mediamtx_bin})")
        print(f"  printer MJPEG → {cfg.printer_host}:{cfg.camera_port}{cfg.camera_path}")
        print("  RTSP URLs (share with VLC / HASS / Jellyfin / Frigate):")
        for url in build_urls(cfg):
            print(f"    {url}")
        print(f"  config: {cfg_path}")
        print("  (Ctrl-C to stop)")

        proc = subprocess.Popen(
            [mediamtx_bin, cfg_path],
            stdout=None,
            stderr=None,
            start_new_session=True,
        )

        def _forward(signum: int, _frame: object) -> None:
            proc.send_signal(signum)

        old_int = signal.signal(signal.SIGINT, _forward)
        old_term = signal.signal(signal.SIGTERM, _forward)
        try:
            return proc.wait()
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(cfg_path)


def start_detached(cfg: RtspConfig) -> tuple[subprocess.Popen[bytes], str]:
    """Spawn MediaMTX as a detached subprocess for supervised lifetimes.

    Returns ``(process, config_path)``. The caller owns cleanup:
    :func:`stop_detached` terminates the process and removes the config file.
    Useful when the HTTP server manages RTSP toggling from the web UI.
    """
    mediamtx_bin, ffmpeg_bin = ensure_binaries(cfg)
    yaml_text = _mediamtx_yaml(cfg, ffmpeg_bin)
    fd, cfg_path = tempfile.mkstemp(prefix="pycentauri-mediamtx-", suffix=".yml", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(yaml_text)
    proc = subprocess.Popen(
        [mediamtx_bin, cfg_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, cfg_path


def stop_detached(
    proc: subprocess.Popen[bytes] | None,
    cfg_path: str | None,
    *,
    timeout: float = 5.0,
) -> None:
    """Clean up a :func:`start_detached` pair. Idempotent."""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)
    if cfg_path:
        with contextlib.suppress(OSError):
            os.unlink(cfg_path)


__all__ = [
    "FFMPEG_HINT",
    "INSTALL_HINT",
    "RtspConfig",
    "RtspError",
    "build_urls",
    "ensure_binaries",
    "find_binary",
    "render_config",
    "run",
    "start_detached",
    "stop_detached",
]
