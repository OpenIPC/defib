"""CLI entry point using Typer + Rich."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="defib",
    help="Universal camera recovery tool - shocking dead devices back to life.",
    no_args_is_help=True,
)


@app.command()
def burn(
    chip: str = typer.Option(..., "-c", "--chip", help="Chip model name"),
    file: str = typer.Option("", "-f", "--file", help="Firmware file (auto-downloads from OpenIPC if omitted)"),
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    send_break: bool = typer.Option(False, "-b", "--break", help="Send Ctrl-C after upload"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json, quiet"),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging"),
) -> None:
    """Recover a device by uploading firmware via UART serial.

    If no firmware file is specified with -f, automatically downloads
    the appropriate U-Boot from OpenIPC releases.
    """
    import asyncio
    asyncio.run(_burn_async(chip, file, port, send_break, output, debug))


async def _burn_async(
    chip: str, file: str, port: str, send_break: bool, output: str, debug: bool
) -> None:
    import json as json_mod
    import logging

    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

    from defib.recovery.events import LogEvent, ProgressEvent
    from defib.recovery.session import RecoverySession

    console = Console()

    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # Resolve firmware: local file or auto-download from OpenIPC
    firmware_path = file
    if not firmware_path:
        from defib.firmware import has_firmware, download_firmware, get_cached_path

        if not has_firmware(chip):
            msg = (
                f"No pre-built firmware for '{chip}' on OpenIPC. "
                f"Specify a local file with -f/--file."
            )
            if output == "json":
                print(json_mod.dumps({"event": "error", "message": msg}))
            else:
                console.print(f"[red]{msg}[/red]")
            raise typer.Exit(1)

        cached = get_cached_path(chip)
        if cached:
            firmware_path = str(cached)
            if output == "human":
                console.print(f"Firmware: [cyan]{cached.name}[/cyan] (cached)")
        else:
            if output == "human":
                console.print(f"Downloading U-Boot for [cyan]{chip}[/cyan] from OpenIPC...")
            elif output == "json":
                print(json_mod.dumps({"event": "download_start", "chip": chip}), flush=True)
            try:
                def _dl_progress(done: int, total: int) -> None:
                    if output == "human":
                        pct = done * 100 // total if total else 0
                        console.print(f"\r  Downloading: {pct}%", end="")
                    elif output == "json" and done == total:
                        print(json_mod.dumps({"event": "download_complete", "bytes": total}), flush=True)

                path = download_firmware(chip, on_progress=_dl_progress)
                firmware_path = str(path)
                if output == "human":
                    console.print(f"\n  Saved: [cyan]{path.name}[/cyan] ({path.stat().st_size} bytes)")
            except (ValueError, ConnectionError) as e:
                if output == "json":
                    print(json_mod.dumps({"event": "error", "message": str(e)}))
                else:
                    console.print(f"\n[red]Download failed:[/red] {e}")
                raise typer.Exit(1)

    try:
        session = RecoverySession(chip=chip, firmware_path=firmware_path)
    except (ValueError, FileNotFoundError) as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": str(e)}))
        else:
            console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if output == "human":
        console.print(f"Protocol: [cyan]{session.protocol_name}[/cyan]")
        console.print(f"Port: [cyan]{port}[/cyan]")

    # Use platform-aware transport factory
    try:
        from defib.transport.serial_platform import create_transport, normalize_port_name
        transport = await create_transport(normalize_port_name(port))
    except Exception as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": f"Serial port error: {e}"}))
        else:
            console.print(f"[red]Failed to open serial port:[/red] {e}")
        raise typer.Exit(2)

    # Rich progress bar for human output
    from rich.progress import TaskID
    progress_ctx = None
    progress_tasks: dict[str, TaskID] = {}

    if output == "human":
        from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TaskProgressColumn, TimeElapsedColumn
        progress_ctx = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        progress_ctx.start()

    def on_progress(event: ProgressEvent) -> None:
        if output == "json":
            print(json_mod.dumps({
                "event": "progress",
                "stage": event.stage.value,
                "bytes_sent": event.bytes_sent,
                "bytes_total": event.bytes_total,
                "message": event.message,
            }), flush=True)
        elif output == "human" and progress_ctx is not None:
            stage_key = event.stage.value
            if event.bytes_total > 1:  # Skip trivial stages
                if stage_key not in progress_tasks:
                    desc = event.message or stage_key
                    progress_tasks[stage_key] = progress_ctx.add_task(
                        desc, total=event.bytes_total,
                    )
                progress_ctx.update(
                    progress_tasks[stage_key], completed=event.bytes_sent,
                )
        elif output == "quiet":
            pass

    def on_log(event: LogEvent) -> None:
        if output == "json":
            print(json_mod.dumps({
                "event": "log",
                "level": event.level,
                "message": event.message,
            }))
        elif output == "human":
            style = {"error": "red", "warn": "yellow", "info": "green"}.get(event.level, "")
            console.print(f"[{style}]{event.message}[/{style}]")

    try:
        result = await session.run(
            transport,
            on_progress=on_progress,
            on_log=on_log,
            send_break=send_break,
        )
    finally:
        if progress_ctx is not None:
            progress_ctx.stop()
        await transport.close()

    if output == "json":
        print(json_mod.dumps({
            "event": "done",
            "success": result.success,
            "elapsed_ms": result.elapsed_ms,
            "error": result.error,
        }))
    elif output == "human":
        if result.success:
            console.print(f"\n[green bold]Done![/green bold] ({result.elapsed_ms:.0f}ms)")
        else:
            console.print(f"\n[red bold]Failed:[/red bold] {result.error}")

    if not result.success:
        raise typer.Exit(1)


@app.command("list-chips")
def list_chips_cmd(
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """List all supported chip models."""
    import json as json_mod
    from rich.console import Console
    from rich.columns import Columns

    from defib.profiles.loader import list_all_chips

    chips = list_all_chips()

    if output == "json":
        print(json_mod.dumps({"chips": chips, "count": len(chips)}))
    else:
        console = Console()
        console.print(f"[bold]{len(chips)} supported chips:[/bold]\n")
        console.print(Columns(chips, column_first=True, padding=(0, 2)))


@app.command()
def ports(
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """List available serial ports."""
    import json as json_mod
    from rich.console import Console
    from rich.table import Table
    from serial.tools.list_ports import comports

    # Filter out ghost/placeholder ports (no USB vendor ID = not a real adapter)
    port_list = sorted(
        [p for p in comports() if p.vid is not None],
        key=lambda p: p.device,
    )

    if output == "json":
        print(json_mod.dumps({
            "ports": [
                {"device": p.device, "description": p.description, "hwid": p.hwid}
                for p in port_list
            ]
        }))
    else:
        console = Console()
        if not port_list:
            console.print("[yellow]No serial ports found[/yellow]")
            return

        table = Table(title="Serial Ports")
        table.add_column("Device", style="cyan")
        table.add_column("Description")
        table.add_column("Hardware ID", style="dim")
        for p in port_list:
            table.add_row(p.device, p.description, p.hwid)
        console.print(table)


@app.command()
def detect(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
    timeout: float = typer.Option(25.0, "--timeout", help="Detection timeout in seconds"),
) -> None:
    """Auto-detect the SoC by trying protocol handshakes."""
    import asyncio
    asyncio.run(_detect_async(port, output, timeout))


async def _detect_async(port: str, output: str, timeout: float) -> None:
    import json as json_mod
    from rich.console import Console

    from defib.protocol.registry import list_protocols
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console()

    if output == "human":
        console.print(f"Detecting SoC on [cyan]{port}[/cyan]...")
        console.print("Power-cycle the device now and wait.\n")

    try:
        transport = await create_transport(normalize_port_name(port))
    except Exception as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": str(e)}))
        else:
            console.print(f"[red]Failed to open serial port:[/red] {e}")
        raise typer.Exit(2)

    try:
        for protocol_cls in list_protocols():
            protocol = protocol_cls()
            if output == "human":
                console.print(f"  Trying {protocol_cls.name()}...")
            try:
                result = await protocol.handshake(transport)
                if result.success:
                    if output == "json":
                        print(json_mod.dumps({
                            "event": "detected",
                            "protocol": protocol_cls.name(),
                            "chip_id": result.chip_id,
                            "board_id": result.board_id,
                            "message": result.message,
                        }))
                    else:
                        console.print(f"\n[green bold]Detected![/green bold] {protocol_cls.name()}")
                        console.print(f"  {result.message}")
                        if result.chip_id:
                            console.print(f"  Chip ID: {hex(result.chip_id)}")
                    return
            except Exception:
                continue
    finally:
        await transport.close()

    if output == "json":
        print(json_mod.dumps({"event": "error", "message": "No SoC detected"}))
    else:
        console.print("\n[yellow]No SoC detected.[/yellow] Make sure the device is in boot mode.")
    raise typer.Exit(1)


@app.command()
def capture(
    port: str = typer.Option(..., "-p", "--port", help="Serial port"),
    output_file: str = typer.Option(..., "-o", "--output", help="Output .dcap file"),
    chip: str = typer.Option("", "-c", "--chip", help="Chip name (metadata only)"),
    duration: float = typer.Option(60.0, "--duration", help="Max capture duration in seconds"),
) -> None:
    """Record a raw UART session to a .dcap file."""
    import asyncio
    asyncio.run(_capture_async(port, output_file, chip, duration))


async def _capture_async(
    port: str, output_file: str, chip: str, duration: float
) -> None:
    import asyncio
    import signal

    from rich.console import Console

    from defib.capture.recorder import RecordingTransport
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console()
    console.print(f"Recording UART on [cyan]{port}[/cyan] → [cyan]{output_file}[/cyan]")
    console.print(f"Duration: {duration}s. Press Ctrl-C to stop early.\n")

    transport = await create_transport(normalize_port_name(port))
    recorder = RecordingTransport(transport, chip=chip)

    stop = False

    def on_sigint(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_sigint)

    try:
        start = asyncio.get_event_loop().time()
        while not stop and (asyncio.get_event_loop().time() - start) < duration:
            try:
                waiting = await recorder.bytes_waiting()
                if waiting > 0:
                    data = await recorder.read(min(waiting, 1024), timeout=0.1)
                    # Display as hex for debugging
                    hex_str = data.hex(" ")
                    console.print(f"[dim]RX ({len(data)}B):[/dim] {hex_str[:80]}")
                else:
                    await asyncio.sleep(0.01)
            except Exception:
                await asyncio.sleep(0.01)
    finally:
        await recorder.close()

    recorder.capture.save(output_file)
    cap = recorder.capture
    console.print(f"\n[green]Saved:[/green] {len(cap.records)} records, "
                  f"TX={cap.tx_bytes}B RX={cap.rx_bytes}B, "
                  f"duration={cap.duration_us / 1_000_000:.1f}s")


@app.command()
def replay(
    capture_file: str = typer.Argument(help="Path to .dcap capture file"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """Display the contents of a .dcap capture file."""
    import json as json_mod
    from rich.console import Console
    from rich.table import Table

    from defib.capture.format import CaptureFile, Direction

    console = Console()

    try:
        cap = CaptureFile.load(capture_file)
    except Exception as e:
        console.print(f"[red]Error loading capture:[/red] {e}")
        raise typer.Exit(1)

    if output == "json":
        print(json_mod.dumps({
            "file": capture_file,
            "chip": cap.chip,
            "baudrate": cap.baudrate,
            "records": len(cap.records),
            "tx_bytes": cap.tx_bytes,
            "rx_bytes": cap.rx_bytes,
            "duration_us": cap.duration_us,
        }))
        return

    console.print(f"[bold]Capture:[/bold] {capture_file}")
    console.print(f"  Chip: {cap.chip or '(unknown)'}")
    console.print(f"  Baud: {cap.baudrate}")
    console.print(f"  Records: {len(cap.records)}")
    console.print(f"  TX: {cap.tx_bytes}B  RX: {cap.rx_bytes}B")
    console.print(f"  Duration: {cap.duration_us / 1_000_000:.3f}s\n")

    table = Table(title="Records (first 50)")
    table.add_column("Time (ms)", style="dim", justify="right")
    table.add_column("Dir", style="bold")
    table.add_column("Len", justify="right")
    table.add_column("Data (hex)")

    for record in cap.records[:50]:
        time_ms = f"{record.timestamp_us / 1000:.1f}"
        direction = "[cyan]TX→[/cyan]" if record.direction == Direction.TX else "[yellow]←RX[/yellow]"
        hex_data = record.data.hex(" ")
        if len(hex_data) > 60:
            hex_data = hex_data[:60] + "..."
        table.add_row(time_ms, direction, str(len(record.data)), hex_data)

    console.print(table)

    if len(cap.records) > 50:
        console.print(f"  [dim]... and {len(cap.records) - 50} more records[/dim]")


@app.command("dump-flash")
def dump_flash_cmd(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    output_file: str = typer.Option("flash_dump.bin", "-o", "--output", help="Output binary file"),
    size: str = typer.Option("", "--size", help="Flash size (e.g., 8MB, 16MB) — auto-detect if empty"),
    output: str = typer.Option("human", "--output-mode", help="Output mode: human, json"),
) -> None:
    """Dump flash contents via U-Boot serial console.

    Requires U-Boot to be running on the device (connect to serial
    console first, or use after 'defib burn').
    """
    import asyncio
    asyncio.run(_dump_flash_async(port, output_file, size, output))


async def _dump_flash_async(port: str, output_file: str, size: str, output: str) -> None:
    import json as json_mod

    from rich.console import Console

    from defib.flashdump import FLASH_SIZES, dump_flash
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console()

    # Parse size
    flash_size = None
    if size:
        size_upper = size.upper()
        if size_upper in FLASH_SIZES:
            flash_size = FLASH_SIZES[size_upper]
        else:
            try:
                flash_size = int(size, 0)
            except ValueError:
                console.print(f"[red]Invalid size:[/red] {size}. Use 8MB, 16MB, 32MB, or hex value.")
                raise typer.Exit(1)

    if output == "human":
        console.print("[bold]Flash Dump[/bold]")
        console.print(f"  Port: [cyan]{port}[/cyan]")
        console.print(f"  Output: [cyan]{output_file}[/cyan]")
        if flash_size:
            console.print(f"  Size: [cyan]{flash_size // (1024*1024)}MB[/cyan]")
        else:
            console.print("  Size: [cyan]auto-detect[/cyan]")

    try:
        transport = await create_transport(normalize_port_name(port))
    except Exception as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": str(e)}))
        else:
            console.print(f"[red]Failed to open serial port:[/red] {e}")
        raise typer.Exit(2)

    def on_progress(done: int, total: int) -> None:
        if output == "json":
            print(json_mod.dumps({
                "event": "dump_progress", "bytes_done": done, "bytes_total": total,
            }), flush=True)
        elif output == "human":
            pct = done * 100 // total if total else 0
            mb_done = done / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            console.print(f"\r  Progress: {mb_done:.1f}/{mb_total:.0f} MB ({pct}%)", end="")

    def on_log(msg: str) -> None:
        if output == "json":
            print(json_mod.dumps({"event": "log", "message": msg}), flush=True)
        else:
            console.print(f"  {msg}")

    try:
        bytes_dumped = await dump_flash(
            transport, output_file,
            flash_size=flash_size,
            on_progress=on_progress, on_log=on_log,
        )
        if output == "human":
            console.print(f"\n\n[green bold]Done![/green bold] {bytes_dumped} bytes saved to {output_file}")
        elif output == "json":
            print(json_mod.dumps({"event": "done", "bytes": bytes_dumped, "file": output_file}))
    except Exception as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": str(e)}))
        else:
            console.print(f"\n[red]Dump failed:[/red] {e}")
        raise typer.Exit(1)
    finally:
        await transport.close()


@app.command()
def tui() -> None:
    """Launch the interactive TUI for device recovery."""
    try:
        from defib.tui.app import run_tui
    except ImportError:
        import typer
        from rich.console import Console
        Console().print(
            "[red]Textual not installed.[/red] Install with: "
            "[cyan]uv pip install 'defib[tui]'[/cyan]"
        )
        raise typer.Exit(1)
    run_tui()


@app.command()
def network(
    file: str = typer.Option(..., "-f", "--file", help="Firmware file to serve via TFTP"),
    nic: str = typer.Option("", "--nic", help="Network interface (auto-detect if empty)"),
    ip: str = typer.Option("192.168.1.10", "--ip", help="Temporary IP to assign to NIC"),
    netmask: str = typer.Option("255.255.255.0", "--netmask", help="Subnet mask"),
    tftp_port: int = typer.Option(69, "--tftp-port", help="TFTP server port"),
    timeout: float = typer.Option(300.0, "--timeout", help="Max wait time for device (seconds)"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json, quiet"),
    skip_ip: bool = typer.Option(False, "--skip-ip", help="Skip IP assignment (already configured)"),
) -> None:
    """Recover a device via network (TFTP). Requires root/admin for IP assignment."""
    import asyncio
    asyncio.run(_network_async(file, nic, ip, netmask, tftp_port, timeout, output, skip_ip))


async def _network_async(
    file: str,
    nic: str,
    ip: str,
    netmask: str,
    tftp_port: int,
    timeout: float,
    output: str,
    skip_ip: bool,
) -> None:
    import json as json_mod

    from rich.console import Console

    from defib.network.ip_manager import list_interfaces, temporary_ip
    from defib.network.tftp_server import start_tftp_server

    console = Console()

    # Load firmware
    try:
        firmware = open(file, "rb").read()
    except FileNotFoundError:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": f"File not found: {file}"}))
        else:
            console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(1)

    if output == "human":
        console.print("[bold]Network Recovery Mode[/bold]")
        console.print(f"  Firmware: [cyan]{file}[/cyan] ({len(firmware)} bytes)")
        console.print(f"  TFTP port: [cyan]{tftp_port}[/cyan]")

    # Determine network interface
    if not nic:
        interfaces = list_interfaces()
        if interfaces:
            nic = interfaces[0]
            if output == "human":
                console.print(f"  Auto-detected NIC: [cyan]{nic}[/cyan]")
        else:
            msg = "No network interfaces found. Specify --nic."
            if output == "json":
                print(json_mod.dumps({"event": "error", "message": msg}))
            else:
                console.print(f"[red]{msg}[/red]")
            raise typer.Exit(1)

    def on_tftp_progress(sent: int, total: int) -> None:
        if output == "json":
            print(json_mod.dumps({
                "event": "tftp_progress",
                "bytes_sent": sent,
                "bytes_total": total,
            }), flush=True)
        elif output == "human":
            pct = (sent / total * 100) if total > 0 else 0
            console.print(f"\r  TFTP: {sent}/{total} bytes ({pct:.0f}%)", end="")

    async def _run_with_ip() -> None:
        # Start TFTP server
        transport, protocol = await start_tftp_server(
            firmware,
            bind_addr=ip if not skip_ip else "0.0.0.0",
            port=tftp_port,
            on_progress=on_tftp_progress,
        )

        if output == "human":
            console.print(f"\n  TFTP server listening on [cyan]{ip}:{tftp_port}[/cyan]")
            console.print("  [yellow]Power-cycle the device now...[/yellow]\n")
        elif output == "json":
            print(json_mod.dumps({"event": "tftp_ready", "ip": ip, "port": tftp_port}))

        try:
            success = await protocol.wait_for_completion(timeout)
            if success:
                if output == "json":
                    print(json_mod.dumps({
                        "event": "done",
                        "success": True,
                        "bytes_sent": protocol.stats.bytes_sent,
                    }))
                elif output == "human":
                    console.print(f"\n\n[green bold]Transfer complete![/green bold] "
                                  f"({protocol.stats.bytes_sent} bytes)")
            else:
                if output == "json":
                    print(json_mod.dumps({"event": "done", "success": False, "error": "Timeout"}))
                elif output == "human":
                    console.print(f"\n\n[yellow]Timeout:[/yellow] No device connected within {timeout}s")
                raise typer.Exit(1)
        finally:
            transport.close()

    if skip_ip:
        await _run_with_ip()
    else:
        if output == "human":
            console.print(f"  Assigning [cyan]{ip}[/cyan] to [cyan]{nic}[/cyan]...")

        try:
            async with temporary_ip(nic, ip, netmask):
                if output == "human":
                    console.print("  [green]IP assigned successfully[/green]")
                await _run_with_ip()
        except Exception as e:
            if "Permission" in str(e) or "Operation not permitted" in str(e):
                msg = f"Permission denied. Run with sudo/admin: {e}"
            else:
                msg = f"Failed to assign IP: {e}"
            if output == "json":
                print(json_mod.dumps({"event": "error", "message": msg}))
            else:
                console.print(f"[red]{msg}[/red]")
            raise typer.Exit(1)


@app.command("list-interfaces")
def list_interfaces_cmd(
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """List available network interfaces."""
    import json as json_mod

    from rich.console import Console

    from defib.network.ip_manager import list_interfaces

    interfaces = list_interfaces()
    if output == "json":
        print(json_mod.dumps({"interfaces": interfaces}))
    else:
        console = Console()
        if interfaces:
            console.print("[bold]Network interfaces:[/bold]")
            for iface in interfaces:
                console.print(f"  [cyan]{iface}[/cyan]")
        else:
            console.print("[yellow]No network interfaces found[/yellow]")


def main() -> None:
    app()
