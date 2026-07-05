"""Typer-based CLI for pycentauri.

All commands accept ``--host`` (or ``PYCENTAURI_HOST``); if none is given,
we run discovery and bail out if there isn't exactly one printer on the
LAN. Control actions additionally require ``--enable-control``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated

import typer

from pycentauri import __version__
from pycentauri.client import Printer
from pycentauri.connect import connect_auto
from pycentauri.discovery import discover as discover_printers

app = typer.Typer(
    name="centauri",
    help="Control and monitor Elegoo Centauri Carbon 3D printers.",
    no_args_is_help=True,
    add_completion=False,
)
print_cmd = typer.Typer(name="print", help="Start, pause, resume, or stop a print.")
app.add_typer(print_cmd, name="print")


HostOpt = Annotated[
    str | None,
    typer.Option(
        "--host",
        "-H",
        envvar="PYCENTAURI_HOST",
        help="Printer IP/hostname. If unset, auto-discover on the LAN.",
    ),
]
ControlOpt = Annotated[
    bool,
    typer.Option(
        "--enable-control",
        envvar="PYCENTAURI_ENABLE_CONTROL",
        help="Required for write actions. Off by default for safety.",
    ),
]
AccessCodeOpt = Annotated[
    str | None,
    typer.Option(
        "--access-code",
        envvar="PYCENTAURI_ACCESS_CODE",
        help="CC2 API key / access code (required for Centauri Carbon 2).",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _echo_err(msg: str) -> None:
    typer.echo(msg, err=True)


async def _resolve_target(host: str | None) -> tuple[str, str | None]:
    """Return (host, mainboard_id). Mainboard_id may be None if discovery failed.

    Always runs a brief UDP discovery so we can pre-seed the mainboard ID on
    ``Printer.connect()`` — the printer doesn't push Attributes in paused/
    errored states, and without a mainboard ID every command would hang.
    """
    if host:
        # Short discovery to learn the mainboard; don't fail if it times out.
        found = await discover_printers(timeout=1.0, retries=2)
        for p in found:
            if p.host == host and p.mainboard_id:
                return host, p.mainboard_id
        return host, None

    found = await discover_printers(timeout=2.5)
    if not found:
        _echo_err("No printers found on the LAN. Pass --host explicitly.")
        raise typer.Exit(code=2)
    if len(found) > 1:
        _echo_err(f"Multiple printers found ({len(found)}); pass --host explicitly.")
        for p in found:
            _echo_err(f"  {p.host}  {p.machine_name or '?'}  {p.firmware_version or '?'}")
        raise typer.Exit(code=2)
    return found[0].host, found[0].mainboard_id


async def _open_printer(
    host: str | None,
    *,
    enable_control: bool = False,
    access_code: str | None = None,
) -> Printer:
    """Resolve host, auto-detect CC1/CC2, and return a connected Printer."""
    h, mid = await _resolve_target(host)
    return await connect_auto(
        h,
        enable_control=enable_control,
        mainboard_id=mid,
        access_code=access_code,
    )


def _run(coro: Coroutine[object, object, object]) -> object:
    return asyncio.run(coro)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Print version and exit.", is_eager=True)
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command("discover")
def cmd_discover(
    timeout: float = typer.Option(2.5, "--timeout", "-t", help="Seconds to wait."),
    as_json: JsonOpt = False,
) -> None:
    """Find Centauri Carbon printers on the local network."""

    async def run() -> None:
        found = await discover_printers(timeout=timeout)
        if as_json:
            payload = [
                {
                    "host": p.host,
                    "mainboard_id": p.mainboard_id,
                    "name": p.name,
                    "machine_name": p.machine_name,
                    "firmware_version": p.firmware_version,
                }
                for p in found
            ]
            typer.echo(json.dumps(payload, indent=2))
        else:
            if not found:
                typer.echo("(no printers responded)")
                return
            for p in found:
                typer.echo(
                    f"{p.host:<15s}  {p.machine_name or '?':<18s}  "
                    f"fw={p.firmware_version or '?':<8s}  id={p.mainboard_id or '?'}"
                )

    _run(run())


@app.command("status")
def cmd_status(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Print the printer's current status once and exit."""

    async def run() -> None:
        async with await _open_printer(host, access_code=access_code) as printer:
            st = await printer.status()
        if as_json:
            typer.echo(json.dumps(st.raw, indent=2, default=str))
        else:
            pi = st.print_info
            typer.echo(f"state        : {st.state} (print_status={st.print_status})")
            typer.echo(
                f"progress     : {st.progress}%" if st.progress is not None else "progress     : ?"
            )
            typer.echo(f"filename     : {st.filename or '-'}")
            typer.echo(
                f"nozzle       : {st.temp_nozzle:.1f}°C / {st.temp_nozzle_target or 0:.0f}°C"
                if st.temp_nozzle is not None
                else "nozzle       : ?"
            )
            typer.echo(
                f"bed          : {st.temp_bed:.1f}°C / {st.temp_bed_target or 0:.0f}°C"
                if st.temp_bed is not None
                else "bed          : ?"
            )
            typer.echo(
                f"chamber      : {st.temp_chamber:.1f}°C / {st.temp_chamber_target or 0:.0f}°C"
                if st.temp_chamber is not None
                else "chamber      : ?"
            )
            if pi is not None and pi.total_layer:
                typer.echo(f"layer        : {pi.current_layer}/{pi.total_layer}")
            if st.coord is not None:
                x, y, z = st.coord
                typer.echo(f"position     : X={x:.2f} Y={y:.2f} Z={z:.2f}")
            if st.z_offset is not None:
                typer.echo(f"z-offset     : {st.z_offset:.3f}")
            if st.fan_speed:
                fans = "  ".join(f"{k}={v}%" for k, v in st.fan_speed.items())
                typer.echo(f"fans         : {fans}")

    _run(run())


@app.command("watch")
def cmd_watch(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    period_ms: int = typer.Option(2000, "--period-ms", help="Push interval."),
    as_json: JsonOpt = False,
) -> None:
    """Stream live status updates until interrupted (Ctrl-C)."""

    async def run() -> None:
        async with await _open_printer(host, access_code=access_code) as printer:
            async for st in printer.watch():
                if as_json:
                    typer.echo(json.dumps(st.raw, default=str))
                else:
                    noz = f"{st.temp_nozzle:.1f}" if st.temp_nozzle is not None else "?"
                    bed = f"{st.temp_bed:.1f}" if st.temp_bed is not None else "?"
                    prog = f"{st.progress}%" if st.progress is not None else "?"
                    typer.echo(
                        f"[{st.print_status or '-'}] {prog:>4s}  nozzle={noz}  bed={bed}  "
                        f"file={st.filename or '-'}"
                    )

    with contextlib.suppress(KeyboardInterrupt):
        _run(run())


@app.command("attributes")
def cmd_attributes(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Print the printer's attributes (model, firmware, capabilities)."""

    async def run() -> None:
        async with await _open_printer(host, access_code=access_code) as printer:
            attrs = await printer.attributes()
        if as_json:
            typer.echo(json.dumps(attrs.raw, indent=2, default=str))
        else:
            typer.echo(f"mainboard_id : {attrs.mainboard_id}")
            typer.echo(f"name         : {attrs.name}")
            typer.echo(f"machine_name : {attrs.machine_name}")
            typer.echo(f"firmware     : {attrs.firmware_version}")
            if attrs.capabilities:
                typer.echo(f"capabilities : {', '.join(attrs.capabilities)}")

    _run(run())


@app.command("snapshot")
def cmd_snapshot(
    out: Annotated[Path, typer.Argument(help="Output JPEG path, or '-' for stdout.")],
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    timeout: float = typer.Option(10.0, "--timeout", "-t"),
) -> None:
    """Save a JPEG snapshot from the built-in webcam."""

    async def run() -> None:
        async with await _open_printer(host, access_code=access_code) as printer:
            jpeg = await printer.snapshot(timeout=timeout)
        if str(out) == "-":
            sys.stdout.buffer.write(jpeg)
        else:
            out.write_bytes(jpeg)
            typer.echo(f"wrote {len(jpeg)} bytes to {out}")

    _run(run())


@print_cmd.command("start")
def cmd_print_start(
    filename: Annotated[str, typer.Argument(help="File name as it appears on the printer.")],
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
    storage: str = typer.Option("local", "--storage", help="'local' or 'udisk'."),
    auto_leveling: bool = typer.Option(True, "--auto-level/--no-auto-level"),
    timelapse: bool = typer.Option(False, "--timelapse/--no-timelapse"),
) -> None:
    """Start a print of a file already on the printer."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            result = await printer.start_print(
                filename, storage=storage, auto_leveling=auto_leveling, timelapse=timelapse
            )
        typer.echo(f"start_print sent; response: {result.inner}")

    _run(run())


@print_cmd.command("pause")
def cmd_print_pause(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Pause the current print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            await printer.pause()
        typer.echo("paused")

    _run(run())


@print_cmd.command("resume")
def cmd_print_resume(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Resume a paused print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            await printer.resume()
        typer.echo("resumed")

    _run(run())


@print_cmd.command("stop")
def cmd_print_stop(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Stop the current print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            await printer.stop()
        typer.echo("stop sent")

    _run(run())


@app.command("speed")
def cmd_speed(
    mode: Annotated[
        str,
        typer.Argument(
            help="Speed mode: silent | balanced | sport | ludicrous "
            "(or the integer 50 | 100 | 130 | 160).",
        ),
    ],
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Set the print-speed mode. Only effective while a print is running."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    parsed: str | int = int(mode) if mode.lstrip("-").isdigit() else mode

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            try:
                await printer.set_print_speed(parsed)
            except ValueError as err:
                _echo_err(str(err))
                raise typer.Exit(code=2) from err
        typer.echo(f"speed mode set: {mode}")

    _run(run())


@app.command("fan")
def cmd_fan(
    model: Annotated[
        int | None,
        typer.Option("--model", help="Model (part-cooling) fan 0..100%."),
    ] = None,
    auxiliary: Annotated[
        int | None,
        typer.Option("--aux", "--auxiliary", help="Auxiliary fan 0..100%."),
    ] = None,
    chamber: Annotated[
        int | None,
        typer.Option("--chamber", help="Chamber/box fan 0..100%."),
    ] = None,
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Set fan speeds. Pass any subset; omitted fans are left untouched."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)
    if model is None and auxiliary is None and chamber is None:
        _echo_err("Specify at least one of --model, --aux, --chamber.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            try:
                await printer.set_fan_speed(model=model, auxiliary=auxiliary, chamber=chamber)
            except ValueError as err:
                _echo_err(str(err))
                raise typer.Exit(code=2) from err
        parts = [
            f"{k}={v}%"
            for k, v in (("model", model), ("aux", auxiliary), ("chamber", chamber))
            if v is not None
        ]
        typer.echo("fans set: " + ", ".join(parts))

    _run(run())


@app.command("temp")
def cmd_temp(
    nozzle: Annotated[
        float | None, typer.Option("--nozzle", help="Nozzle target °C (0 = off).")
    ] = None,
    bed: Annotated[float | None, typer.Option("--bed", help="Bed target °C (0 = off).")] = None,
    chamber: Annotated[
        float | None, typer.Option("--chamber", help="Chamber target °C (0 = off).")
    ] = None,
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Set heater target temperatures. Pass any subset."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)
    if nozzle is None and bed is None and chamber is None:
        _echo_err("Specify at least one of --nozzle, --bed, --chamber.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            try:
                await printer.set_temperatures(nozzle=nozzle, bed=bed, chamber=chamber)
            except ValueError as err:
                _echo_err(str(err))
                raise typer.Exit(code=2) from err
        parts = [
            f"{k}={v}°C"
            for k, v in (("nozzle", nozzle), ("bed", bed), ("chamber", chamber))
            if v is not None
        ]
        typer.echo("targets set: " + ", ".join(parts))

    _run(run())


@app.command("canvas")
def cmd_canvas(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Show Canvas multi-filament system status (CC2 only)."""

    async def run() -> None:
        async with await _open_printer(host, access_code=access_code) as printer:
            cs = await printer.canvas_status()
        if as_json:
            typer.echo(json.dumps(cs.raw, indent=2, default=str))
        else:
            typer.echo(f"auto_refill  : {'ON' if cs.auto_refill else 'OFF'}")
            typer.echo(f"active_tray  : {cs.active_tray_id if cs.active_tray_id >= 0 else 'none'}")
            typer.echo(f"connected    : {'yes' if cs.connected else 'no'}")
            for unit in cs.canvas_list:
                typer.echo(f"\ncanvas #{unit.canvas_id}:")
                for t in unit.tray_list:
                    loaded = "●" if t.status == 1 else "○"
                    typer.echo(
                        f"  {loaded} tray {t.tray_id}: {t.filament_name} "
                        f"({t.filament_type}) {t.filament_color} "
                        f"[{t.min_nozzle_temp}-{t.max_nozzle_temp}°C]"
                    )

    _run(run())


@app.command("refill")
def cmd_refill(
    on: Annotated[bool, typer.Option("--on/--off", help="Enable or disable auto-refill.")] = True,
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    enable_control: ControlOpt = False,
) -> None:
    """Toggle Canvas auto-refill (CC2 only)."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        async with await _open_printer(
            host, enable_control=True, access_code=access_code
        ) as printer:
            await printer.set_auto_refill(on)
        typer.echo(f"auto-refill {'enabled' if on else 'disabled'}")

    _run(run())


@app.command("server")
def cmd_server(
    host: HostOpt = None,
    access_code: AccessCodeOpt = None,
    bind: str = typer.Option(
        "127.0.0.1", "--bind", help="Interface to bind. Use 0.0.0.0 to expose on LAN."
    ),
    port: int = typer.Option(8787, "--port", "-p"),
    enable_control: ControlOpt = False,
    log_level: str = typer.Option("info", "--log-level"),
    rtsp: bool = typer.Option(
        False,
        "--rtsp/--no-rtsp",
        help="Enable /api/rtsp/* endpoints and the STREAM panel in the web UI.",
    ),
    rtsp_port: int = typer.Option(8554, "--rtsp-port", help="RTSP port (when --rtsp)."),
    rtsp_path: str = typer.Option("printer", "--rtsp-path", help="RTSP URL path."),
    rtsp_bind: str = typer.Option(
        "0.0.0.0", "--rtsp-bind", help="Interface MediaMTX binds to for RTSP."
    ),
    rtsp_fps: int = typer.Option(15, "--rtsp-fps"),
    rtsp_bitrate: str = typer.Option("2M", "--rtsp-bitrate"),
) -> None:
    """Run the HTTP + SSE server (requires `pip install 'pycentauri[server]'`)."""
    try:
        from pycentauri.server import run as run_server
    except ImportError as err:
        _echo_err("Server support not installed. Install with: pip install 'pycentauri[server]'")
        _echo_err(f"(missing dependency: {err})")
        raise typer.Exit(code=1) from err

    async def resolve() -> tuple[str, str | None]:
        return await _resolve_target(host)

    h, mid = asyncio.run(resolve())

    rtsp_cfg = None
    if rtsp:
        from pycentauri.rtsp import RtspConfig

        rtsp_cfg = RtspConfig(
            printer_host=h,
            rtsp_port=rtsp_port,
            bind=rtsp_bind,
            path=rtsp_path,
            fps=rtsp_fps,
            bitrate=rtsp_bitrate,
        )

    run_server(
        h,
        bind=bind,
        port=port,
        enable_control=enable_control,
        mainboard_id=mid,
        access_code=access_code,
        log_level=log_level,
        rtsp_config=rtsp_cfg,
    )


@app.command("rtsp")
def cmd_rtsp(
    host: HostOpt = None,
    port: int = typer.Option(8554, "--port", "-p", help="RTSP TCP port."),
    bind: str = typer.Option("0.0.0.0", "--bind", help="Interface to bind MediaMTX on."),
    stream_path: str = typer.Option("printer", "--path", help="RTSP path, e.g. rtsp://.../<path>"),
    fps: int = typer.Option(15, "--fps", help="Re-encode frame rate cap."),
    bitrate: str = typer.Option("2M", "--bitrate", help="ffmpeg video bitrate (e.g. 2M, 4M)."),
    preset: str = typer.Option("veryfast", "--preset", help="libx264 preset."),
    mediamtx_path: str = typer.Option(
        None, "--mediamtx-path", help="Override mediamtx binary path."
    ),
    ffmpeg_path: str = typer.Option(None, "--ffmpeg-path", help="Override ffmpeg binary path."),
    enable_webrtc: bool = typer.Option(
        False, "--webrtc/--no-webrtc", help="Also serve WebRTC (MediaMTX defaults)."
    ),
    enable_hls: bool = typer.Option(False, "--hls/--no-hls", help="Also serve HLS."),
) -> None:
    """Re-stream the printer's MJPEG webcam as RTSP/H.264 via MediaMTX.

    Requires ``mediamtx`` and ``ffmpeg`` on $PATH (or supply --*-path).
    Transcoding only happens while a client is actually connected to the
    RTSP URL, so idle cost is zero.
    """
    from pycentauri.camera import CAMERA_PORT, CAMERA_PORT_CC2
    from pycentauri.connect import _port_open
    from pycentauri.rtsp import RtspConfig, RtspError, run

    async def resolve() -> tuple[str, str | None, int]:
        h, mid = await _resolve_target(host)
        # CC1 serves MJPEG on :3031, CC2 on :8080. Probing :1883 alone is
        # conclusive (CC1 never runs MQTT) and a CC1 answers it with a
        # harmless kernel RST — we deliberately never probe :3030, which
        # is sensitive to connect/close churn.
        cam = CAMERA_PORT_CC2 if await _port_open(h, 1883) else CAMERA_PORT
        return h, mid, cam

    h, _mid, cam_port = asyncio.run(resolve())

    cfg = RtspConfig(
        printer_host=h,
        camera_port=cam_port,
        rtsp_port=port,
        bind=bind,
        path=stream_path,
        fps=fps,
        bitrate=bitrate,
        preset=preset,
        mediamtx_path=mediamtx_path,
        ffmpeg_path=ffmpeg_path,
        enable_webrtc=enable_webrtc,
        enable_hls=enable_hls,
    )
    try:
        exit_code = run(cfg)
    except RtspError as err:
        _echo_err(str(err))
        raise typer.Exit(code=1) from err
    except KeyboardInterrupt:
        exit_code = 0
    raise typer.Exit(code=exit_code)


@app.command("mcp")
def cmd_mcp(
    enable_control: ControlOpt = False,
    host: HostOpt = None,
) -> None:
    """Run the MCP stdio server (`python -m pycentauri.mcp` does the same)."""
    try:
        from pycentauri.mcp.server import run_stdio
    except ImportError as e:
        _echo_err("MCP support not installed. Install with: pip install 'pycentauri[mcp]'")
        _echo_err(f"(missing dependency: {e})")
        raise typer.Exit(code=1) from e
    if host:
        os.environ["PYCENTAURI_HOST"] = host
    run_stdio(enable_control=enable_control)
