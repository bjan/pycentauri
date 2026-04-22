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
from pathlib import Path
from typing import Annotated

import typer

from pycentauri import __version__
from pycentauri.client import Printer
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


def _run(coro: asyncio.coroutines.Coroutine[object, object, object]) -> object:  # type: ignore[name-defined]
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
def cmd_status(host: HostOpt = None, as_json: JsonOpt = False) -> None:
    """Print the printer's current status once and exit."""

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, mainboard_id=mid) as printer:
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
    period_ms: int = typer.Option(2000, "--period-ms", help="Push interval."),
    as_json: JsonOpt = False,
) -> None:
    """Stream live status updates until interrupted (Ctrl-C)."""

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, push_period_ms=period_ms, mainboard_id=mid) as printer:
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
def cmd_attributes(host: HostOpt = None, as_json: JsonOpt = False) -> None:
    """Print the printer's attributes (model, firmware, capabilities)."""

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, mainboard_id=mid) as printer:
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
    timeout: float = typer.Option(10.0, "--timeout", "-t"),
) -> None:
    """Save a JPEG snapshot from the built-in webcam."""

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, mainboard_id=mid) as printer:
            jpeg = await printer.snapshot(timeout=timeout)
        if str(out) == "-":
            sys.stdout.buffer.write(jpeg)
        else:
            out.write_bytes(jpeg)
            typer.echo(f"wrote {len(jpeg)} bytes to {out}")

    _run(run())


@app.command("files")
def cmd_files(host: HostOpt = None, as_json: JsonOpt = False) -> None:
    """List files stored on the printer.

    Note: the file-list command isn't wired in v0.1 (the upstream SDK also
    marks it as not-implemented on CC); this is a stub so the command name
    is reserved.
    """

    _echo_err("file listing is not yet supported on the original Centauri Carbon firmware")
    raise typer.Exit(code=1)


@print_cmd.command("start")
def cmd_print_start(
    filename: Annotated[str, typer.Argument(help="File name as it appears on the printer.")],
    host: HostOpt = None,
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
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, enable_control=True, mainboard_id=mid) as printer:
            result = await printer.start_print(
                filename, storage=storage, auto_leveling=auto_leveling, timelapse=timelapse
            )
        typer.echo(f"start_print sent; response: {result.inner}")

    _run(run())


@print_cmd.command("pause")
def cmd_print_pause(host: HostOpt = None, enable_control: ControlOpt = False) -> None:
    """Pause the current print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, enable_control=True, mainboard_id=mid) as printer:
            await printer.pause()
        typer.echo("paused")

    _run(run())


@print_cmd.command("resume")
def cmd_print_resume(host: HostOpt = None, enable_control: ControlOpt = False) -> None:
    """Resume a paused print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, enable_control=True, mainboard_id=mid) as printer:
            await printer.resume()
        typer.echo("resumed")

    _run(run())


@print_cmd.command("stop")
def cmd_print_stop(host: HostOpt = None, enable_control: ControlOpt = False) -> None:
    """Stop the current print."""
    if not enable_control:
        _echo_err("Refusing to send a write action without --enable-control.")
        raise typer.Exit(code=2)

    async def run() -> None:
        h, mid = await _resolve_target(host)
        async with await Printer.connect(h, enable_control=True, mainboard_id=mid) as printer:
            await printer.stop()
        typer.echo("stop sent")

    _run(run())


@app.command("server")
def cmd_server(
    host: HostOpt = None,
    bind: str = typer.Option(
        "127.0.0.1", "--bind", help="Interface to bind. Use 0.0.0.0 to expose on LAN."
    ),
    port: int = typer.Option(8787, "--port", "-p"),
    enable_control: ControlOpt = False,
    log_level: str = typer.Option("info", "--log-level"),
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
    run_server(
        h,
        bind=bind,
        port=port,
        enable_control=enable_control,
        mainboard_id=mid,
        log_level=log_level,
    )


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
