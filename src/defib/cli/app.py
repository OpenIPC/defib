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
    terminal: bool = typer.Option(False, "-t", "--terminal", help="Open serial terminal after upload"),
    power_cycle: bool = typer.Option(False, "--power-cycle", help="Auto power-cycle via PoE (needs DEFIB_POE_* env vars)"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json, quiet"),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging"),
) -> None:
    """Recover a device by uploading firmware via UART serial.

    If no firmware file is specified with -f, automatically downloads
    the appropriate U-Boot from OpenIPC releases.
    """
    import asyncio
    asyncio.run(_burn_async(chip, file, port, send_break, terminal, power_cycle, output, debug))


async def _burn_async(
    chip: str, file: str, port: str, send_break: bool, terminal: bool,
    power_cycle: bool, output: str, debug: bool,
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

    # Power controller setup
    power_controller = None
    poe_port = None
    if power_cycle:
        from defib.power.routeros import RouterOSController

        try:
            power_controller = RouterOSController.from_env()
        except Exception as e:
            if output == "json":
                print(json_mod.dumps({"event": "error", "message": str(e)}))
            else:
                console.print(f"[red]Power controller error:[/red] {e}")
            raise typer.Exit(1)

        # Extract device label from serial port name for auto-discovery
        # e.g. /dev/uart-IVGHP203Y-AF -> IVGHP203Y-AF
        from pathlib import Path
        port_basename = Path(port).name
        device_label = port_basename.removeprefix("uart-") if port_basename.startswith("uart-") else port_basename

        try:
            poe_port = await power_controller.find_port_by_comment(device_label)
        except Exception as e:
            if output == "json":
                print(json_mod.dumps({"event": "error", "message": str(e)}))
            else:
                console.print(f"[red]PoE port discovery failed:[/red] {e}")
            await power_controller.close()
            raise typer.Exit(1)

        if output == "human":
            console.print(f"PoE control: [cyan]{poe_port}[/cyan] on [cyan]{power_controller._host}[/cyan]")

    try:
        session = RecoverySession(
            chip=chip, firmware_path=firmware_path,
            power_controller=power_controller, poe_port=poe_port,
        )
    except (ValueError, FileNotFoundError) as e:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": str(e)}))
        else:
            console.print(f"[red]Error:[/red] {e}")
        if power_controller:
            await power_controller.close()
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
        if power_controller:
            await power_controller.close()

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
        await transport.close()
        raise typer.Exit(1)

    # Terminal mode: stream serial output until Ctrl-C
    # Auto-detects download_process mode and bridges XHEAD/XCMD framing.
    if terminal and result.success:
        import signal
        import sys as _sys

        # Read initial output to detect which mode U-Boot entered
        boot_buf = bytearray()
        for _ in range(60):  # up to 6 seconds
            try:
                data = await transport.read(256, timeout=0.1)
                boot_buf.extend(data)
                _sys.stdout.buffer.write(data)
                _sys.stdout.buffer.flush()
            except Exception:
                pass
            if b"start download process" in boot_buf or b"hisilicon #" in boot_buf or b"OpenIPC #" in boot_buf:
                break

        download_mode = b"start download process" in boot_buf

        if download_mode:
            if output == "human":
                console.print("\n[dim]--- Download command mode (type U-Boot commands, Ctrl-C to exit) ---[/dim]")

            from defib.protocol.download_cmd import DownloadCommandClient
            client = DownloadCommandClient(transport)

            stop = False

            def on_sigint(*_: object) -> None:
                nonlocal stop
                stop = True

            signal.signal(signal.SIGINT, on_sigint)

            import asyncio as _asyncio
            try:
                while not stop:
                    try:
                        cmd = await _asyncio.get_event_loop().run_in_executor(
                            None, lambda: input("defib> ")
                        )
                    except (EOFError, OSError):
                        break
                    if not cmd.strip():
                        continue
                    ok, out = await client.send_command(cmd.strip(), timeout=120)
                    if out.strip():
                        print(out.strip())
                    if ok:
                        print("[OK]")
                    else:
                        print("[ERROR]")
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                if output == "human":
                    console.print("[dim]--- Session closed ---[/dim]")
        else:
            # Normal U-Boot shell — raw terminal passthrough
            if output == "human":
                console.print("[dim]--- Terminal mode (Ctrl-C to exit) ---[/dim]")

            stop = False

            def on_sigint(*_: object) -> None:
                nonlocal stop
                stop = True

            signal.signal(signal.SIGINT, on_sigint)

            try:
                while not stop:
                    try:
                        data = await transport.read(256, timeout=0.1)
                        _sys.stdout.buffer.write(data)
                        _sys.stdout.buffer.flush()
                    except Exception:
                        pass
            finally:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                if output == "human":
                    console.print("\n[dim]--- Terminal closed ---[/dim]")

    await transport.close()


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
    from defib.serial_ports import list_serial_ports

    port_list = list_serial_ports()

    if output == "json":
        print(json_mod.dumps({
            "ports": [
                {
                    "device": p.device,
                    "description": p.description,
                    "hwid": p.hwid,
                    "alias_device": p.alias_device,
                    "open_path": p.open_path,
                    "display_name": p.display_name,
                    "manufacturer": p.manufacturer,
                    "product": p.product,
                    "serial_number": p.serial_number,
                    "location": p.location,
                    "vid": p.vid,
                    "pid": p.pid,
                }
                for p in port_list
            ]
        }))
    else:
        console = Console()
        if not port_list:
            console.print("[yellow]No serial ports found[/yellow]")
            return

        table = Table(title="Serial Ports")
        table.add_column("Alias", style="green")
        table.add_column("Device", style="cyan")
        table.add_column("Identity")
        table.add_column("Location", style="magenta")
        table.add_column("Serial", style="yellow")
        for p in port_list:
            identity = " ".join(part for part in (p.manufacturer, p.product) if part) or p.description
            table.add_row(
                p.alias_device or "",
                p.device,
                identity,
                p.location or "",
                p.serial_number or "",
            )
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


agent_app = typer.Typer(help="Flash agent commands (fast binary protocol)")
app.add_typer(agent_app, name="agent")


@agent_app.command("upload")
def agent_upload(
    chip: str = typer.Option(..., "-c", "--chip", help="Chip model name"),
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """Upload flash agent to device via boot protocol (requires power-cycle)."""
    import asyncio
    asyncio.run(_agent_upload_async(chip, port, output))


async def _agent_upload_async(chip: str, port: str, output: str) -> None:
    import json as json_mod

    from rich.console import Console

    from defib.agent.client import FlashAgentClient, get_agent_binary
    from defib.firmware import get_cached_path
    from defib.profiles.loader import load_profile
    from defib.protocol.hisilicon_standard import HiSiliconStandard
    from defib.recovery.events import ProgressEvent
    from defib.transport.serial import SerialTransport

    console = Console()

    # Find agent binary
    agent_path = get_agent_binary(chip)
    if not agent_path:
        msg = f"No agent binary for '{chip}'"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)

    agent_data = agent_path.read_bytes()

    # Get real SPL from cached U-Boot
    profile = load_profile(chip)
    cached_fw = get_cached_path(chip)
    if not cached_fw:
        from defib.firmware import download_firmware
        if output == "human":
            console.print("Downloading U-Boot for SPL...")
        cached_fw = download_firmware(chip)
    spl_data = cached_fw.read_bytes()[:profile.spl_max_size]

    if output == "human":
        console.print(f"Agent: [cyan]{agent_path.name}[/cyan] ({len(agent_data)} bytes)")
        console.print(f"SPL: {len(spl_data)} bytes (from OpenIPC U-Boot)")
        console.print("\n[yellow]Power-cycle the camera now![/yellow]\n")

    transport = await SerialTransport.create(port)
    protocol = HiSiliconStandard()
    protocol.set_profile(profile)

    def on_progress(e: ProgressEvent) -> None:
        if e.message:
            if output == "human":
                console.print(f"  {e.message}")
            elif output == "json":
                print(json_mod.dumps({"event": "progress", "message": e.message}), flush=True)

    hs = await protocol.handshake(transport, on_progress)
    if not hs.success:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": "Handshake failed"}))
        else:
            console.print("[red]Handshake failed[/red]")
        await transport.close()
        raise typer.Exit(1)

    result = await protocol.send_firmware(
        transport, agent_data, on_progress, spl_override=spl_data,
        payload_label="Agent",
    )
    if not result.success:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": result.error or "Upload failed"}))
        else:
            console.print(f"[red]Upload failed:[/red] {result.error}")
        await transport.close()
        raise typer.Exit(1)

    if output == "human":
        console.print("[green]Agent uploaded![/green] Waiting for READY...")

    # Wait for agent
    import asyncio as aio
    await transport.close()
    await aio.sleep(2)
    transport = await SerialTransport.create(port)

    client = FlashAgentClient(transport, chip)
    if await client.connect(timeout=10.0):
        info = await client.get_info()
        if output == "human":
            console.print("[green bold]Agent ready![/green bold]")
            console.print(f"  RAM: 0x{info.get('ram_base', 0):08x}")
            console.print(f"  Flash: {int(info.get('flash_size', 0)) // 1024}KB")
        elif output == "json":
            print(json_mod.dumps({"event": "ready", **info}))
    else:
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": "Agent not responding"}))
        else:
            console.print("[red]Agent not responding[/red]")
        raise typer.Exit(1)

    await transport.close()


@agent_app.command("flash")
def agent_flash(
    chip: str = typer.Option(..., "-c", "--chip", help="Chip model name"),
    input_file: str = typer.Option(..., "-i", "--input", help="Firmware binary file"),
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="CRC32 verify after write"),
    reboot: bool = typer.Option(True, "--reboot/--no-reboot", help="Reboot after flash"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """Flash firmware in one step: upload agent, write flash, verify, reboot.

    Power-cycle the camera when prompted. The command handles everything:
    boot protocol upload, high-speed UART, streaming flash write with 0xFF
    sector skip, CRC32 verification, and device reboot.
    """
    import asyncio
    asyncio.run(_agent_flash_async(chip, input_file, port, verify, reboot, output))


async def _agent_flash_async(
    chip: str, input_file: str, port: str,
    verify: bool, reboot_device: bool, output: str,
) -> None:
    import json as json_mod
    import time
    import zlib
    from pathlib import Path

    from rich.console import Console

    from defib.agent.client import FlashAgentClient, get_agent_binary
    from defib.firmware import get_cached_path
    from defib.profiles.loader import load_profile
    from defib.protocol.hisilicon_standard import HiSiliconStandard
    from defib.recovery.events import ProgressEvent
    from defib.transport.serial import SerialTransport

    console = Console()
    FLASH_MEM = 0x14000000

    # --- Load firmware file ---
    fw_path = Path(input_file)
    if not fw_path.exists():
        msg = f"Firmware file not found: {input_file}"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)

    firmware = fw_path.read_bytes()
    fw_crc = zlib.crc32(firmware) & 0xFFFFFFFF

    # --- Find agent binary ---
    agent_path = get_agent_binary(chip)
    if not agent_path:
        msg = f"No agent binary for '{chip}'"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)

    agent_data = agent_path.read_bytes()

    # --- Get SPL from cached U-Boot ---
    profile = load_profile(chip)
    cached_fw = get_cached_path(chip)
    if not cached_fw:
        from defib.firmware import download_firmware
        if output == "human":
            console.print("Downloading U-Boot for SPL...")
        cached_fw = download_firmware(chip)
    spl_data = cached_fw.read_bytes()[:profile.spl_max_size]

    if output == "human":
        console.print(f"Firmware: [cyan]{fw_path.name}[/cyan] ({len(firmware)} bytes, CRC {fw_crc:#010x})")
        console.print(f"Agent: [cyan]{agent_path.name}[/cyan] ({len(agent_data)} bytes)")
        console.print("\n[yellow]Power-cycle the camera now![/yellow]\n")

    # --- Phase 1: Upload agent via boot protocol ---
    transport = await SerialTransport.create(port)
    protocol = HiSiliconStandard()
    protocol.set_profile(profile)

    def on_boot_progress(e: ProgressEvent) -> None:
        if e.message:
            if output == "human":
                console.print(f"  {e.message}")
            elif output == "json":
                print(json_mod.dumps({"event": "boot", "message": e.message}), flush=True)

    hs = await protocol.handshake(transport, on_boot_progress)
    if not hs.success:
        msg = "Handshake failed"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        await transport.close()
        raise typer.Exit(1)

    result = await protocol.send_firmware(
        transport, agent_data, on_boot_progress, spl_override=spl_data,
        payload_label="Agent",
    )
    if not result.success:
        msg = result.error or "Upload failed"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]Upload failed:[/red] {msg}")
        await transport.close()
        raise typer.Exit(1)

    # --- Phase 2: Connect to agent ---
    import asyncio as aio
    await transport.close()
    await aio.sleep(2)
    transport = await SerialTransport.create(port)

    client = FlashAgentClient(transport, chip)
    if not await client.connect(timeout=10.0):
        msg = "Agent not responding"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        await transport.close()
        raise typer.Exit(1)

    info = await client.get_info()
    flash_size = int(info.get("flash_size", 0))

    if output == "human":
        console.print(f"[green]Agent ready![/green] Flash: {flash_size // 1024}KB")

    if flash_size > 0 and len(firmware) > flash_size:
        msg = f"Firmware ({len(firmware)} bytes) exceeds flash size ({flash_size} bytes)"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        await transport.close()
        raise typer.Exit(1)

    # --- Phase 3: Flash firmware ---
    if output == "human":
        console.print(f"Flashing {len(firmware)} bytes...")

    t0 = time.monotonic()
    last_pct = [0]

    def on_flash_progress(done: int, total: int) -> None:
        pct = done * 100 // total if total > 0 else 0
        if pct >= last_pct[0] + 5:
            elapsed = time.monotonic() - t0
            speed = done / elapsed if elapsed > 0 else 0
            if output == "human":
                print(f"\r  {pct}% ({done // 1024}KB / {total // 1024}KB) "
                      f"{speed:.0f} B/s", end="", flush=True)
            elif output == "json":
                print(json_mod.dumps({"event": "flash", "pct": pct, "speed": int(speed)}),
                      flush=True)
            last_pct[0] = pct

    ok = await client.write_flash(0, firmware, on_progress=on_flash_progress)
    elapsed = time.monotonic() - t0

    if output == "human":
        print()  # newline after progress
    if not ok:
        msg = f"Flash write failed after {elapsed:.1f}s"
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": msg}))
        else:
            console.print(f"[red]{msg}[/red]")
        await transport.close()
        raise typer.Exit(1)

    speed = len(firmware) / elapsed if elapsed > 0 else 0
    if output == "human":
        console.print(f"  Written in {elapsed:.1f}s ({speed:.0f} B/s)")

    # --- Phase 4: Verify ---
    if verify:
        if output == "human":
            console.print("  Verifying CRC32...")
        dev_crc = await client.crc32(FLASH_MEM, len(firmware))
        match = dev_crc == fw_crc
        if output == "human":
            if match:
                console.print(f"  CRC32: [green]OK[/green] ({fw_crc:#010x})")
            else:
                console.print(f"  CRC32: [red]MISMATCH[/red] (device {dev_crc:#010x} != {fw_crc:#010x})")
        if not match:
            await transport.close()
            raise typer.Exit(1)

    # --- Phase 5: Reboot ---
    if reboot_device:
        if output == "human":
            console.print("  Rebooting...")
        await client.reboot()

    if output == "human":
        console.print(f"\n[green bold]Done![/green bold] Firmware flashed in {elapsed:.0f}s")
    elif output == "json":
        print(json_mod.dumps({
            "event": "done",
            "bytes": len(firmware),
            "elapsed": round(elapsed, 1),
            "speed": int(speed),
            "verified": verify,
            "rebooted": reboot_device,
        }))

    await transport.close()


@agent_app.command("info")
def agent_info(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """Query info from a running flash agent."""
    import asyncio
    asyncio.run(_agent_info_async(port, output))


async def _agent_info_async(port: str, output: str) -> None:
    import json as json_mod

    from rich.console import Console

    from defib.agent.client import FlashAgentClient
    from defib.transport.serial import SerialTransport

    console = Console()
    transport = await SerialTransport.create(port)
    client = FlashAgentClient(transport)

    if not await client.connect(timeout=5.0):
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": "Agent not responding"}))
        else:
            console.print("[red]Agent not responding.[/red] Upload agent first with 'defib agent upload'.")
        await transport.close()
        raise typer.Exit(1)

    info = await client.get_info()
    await transport.close()

    if output == "json":
        print(json_mod.dumps(info))
    else:
        console.print("[bold]Flash Agent Info[/bold]")
        console.print(f"  JEDEC ID:    {info.get('jedec_id', 'unknown')}")
        console.print(f"  Flash size:  {int(info.get('flash_size', 0)) // 1024} KB")
        console.print(f"  RAM base:    0x{int(info.get('ram_base', 0)):08x}")
        console.print(f"  Sector size: {int(info.get('sector_size', 0)) // 1024} KB")
        if "agent_version" in info:
            caps = int(info.get("capabilities", 0))
            cap_names = []
            cap_map = [
                (1 << 0, "flash_stream"), (1 << 1, "sector_bitmap"),
                (1 << 2, "page_skip"), (1 << 3, "set_baud"),
                (1 << 4, "reboot"), (1 << 5, "selfupdate"),
                (1 << 6, "scan"),
            ]
            for bit, name in cap_map:
                if caps & bit:
                    cap_names.append(name)
            console.print(f"  Agent ver:   {info['agent_version']}")
            console.print(f"  Capabilities: {', '.join(cap_names) or 'none'}")


@agent_app.command("read")
def agent_read(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    addr: str = typer.Option(None, "-a", "--addr", help="Start address (hex, default: flash base 0x14000000)"),
    size: str = typer.Option(None, "-s", "--size", help="Size in bytes (or 1KB, 16MB, etc; default: auto-detect)"),
    output_file: str = typer.Option("flash_dump.bin", "-o", "--output", help="Output binary file"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="CRC32 verify after read"),
    output: str = typer.Option("human", "--output-mode", help="Output mode: human, json"),
) -> None:
    """Read memory from device via flash agent."""
    import asyncio
    asyncio.run(_agent_read_async(port, addr, size, output_file, verify, output))


async def _agent_read_async(
    port: str, addr_str: str | None, size_str: str | None, output_file: str, verify: bool, output: str,
) -> None:
    import json as json_mod
    import time
    import zlib

    from rich.console import Console

    from defib.agent.client import FlashAgentClient
    from defib.transport.serial import SerialTransport

    console = Console()

    transport = await SerialTransport.create(port)
    client = FlashAgentClient(transport)
    if not await client.connect(timeout=5.0):
        console.print("[red]Agent not responding[/red]")
        await transport.close()
        raise typer.Exit(1)

    # Default to full flash dump when address/size not specified
    if addr_str is None or size_str is None:
        info = await client.get_info()
        if not info:
            console.print("[red]Failed to get device info[/red]")
            await transport.close()
            raise typer.Exit(1)

    address = int(addr_str, 0) if addr_str is not None else 0x14000000
    size = _parse_size(size_str) if size_str is not None else int(info["flash_size"])

    if output == "human":
        console.print(f"Reading 0x{address:08x} + {size} bytes...")

    t0 = time.time()
    data = await client.read_memory(address, size, on_progress=lambda d, t: (
        print(f"\r  {d}/{t} ({d*100//t}%)", end="", flush=True) if output == "human" else None
    ))
    elapsed = time.time() - t0

    from pathlib import Path
    Path(output_file).write_bytes(data)

    speed = len(data) / elapsed if elapsed > 0 else 0
    if output == "human":
        console.print(f"\n  {len(data)} bytes in {elapsed:.1f}s ({speed:.0f} B/s)")

    if verify and len(data) > 0:
        local_crc = zlib.crc32(data) & 0xFFFFFFFF
        device_crc = await client.crc32(address, len(data))
        match = local_crc == device_crc
        if output == "human":
            console.print(f"  CRC32: {'[green]OK[/green]' if match else '[red]MISMATCH[/red]'}")
        if not match:
            await transport.close()
            raise typer.Exit(1)

    if output == "human":
        console.print(f"[green]Saved:[/green] {output_file}")
    elif output == "json":
        print(json_mod.dumps({"file": output_file, "bytes": len(data), "speed": speed}))

    await transport.close()


@agent_app.command("write")
def agent_write(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    addr: str = typer.Option("0x14000000", "-a", "--addr", help="Start address (hex, default: flash base)"),
    input_file: str = typer.Option(..., "-i", "--input", help="Input binary file"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="CRC32 verify after write"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
) -> None:
    """Write data to device memory via flash agent."""
    import asyncio
    asyncio.run(_agent_write_async(port, addr, input_file, verify, output))


async def _agent_write_async(
    port: str, addr_str: str, input_file: str, verify: bool, output: str,
) -> None:
    import json as json_mod
    import time

    from rich.console import Console

    from defib.agent.client import FlashAgentClient
    from defib.transport.serial import SerialTransport

    console = Console()
    address = int(addr_str, 0)
    data = open(input_file, "rb").read()

    transport = await SerialTransport.create(port)
    client = FlashAgentClient(transport)
    if not await client.connect(timeout=5.0):
        console.print("[red]Agent not responding[/red]")
        await transport.close()
        raise typer.Exit(1)

    if output == "human":
        console.print(f"Writing {len(data)} bytes to 0x{address:08x}...")

    t0 = time.time()
    ok = await client.write_memory(address, data, on_progress=lambda d, t: (
        print(f"\r  {d}/{t} ({d*100//t}%)", end="", flush=True) if output == "human" else None
    ))
    elapsed = time.time() - t0

    if not ok:
        if output == "human":
            console.print("\n[red]Write failed[/red]")
        await transport.close()
        raise typer.Exit(1)

    speed = len(data) / elapsed if elapsed > 0 else 0
    if output == "human":
        console.print(f"\n  {len(data)} bytes in {elapsed:.1f}s ({speed:.0f} B/s)")

    if verify:
        match = await client.verify(address, data)
        if output == "human":
            console.print(f"  CRC32: {'[green]OK[/green]' if match else '[red]MISMATCH[/red]'}")
        if not match:
            await transport.close()
            raise typer.Exit(1)

    if output == "human":
        console.print("[green]Write complete[/green]")
    elif output == "json":
        print(json_mod.dumps({"bytes": len(data), "speed": speed, "verified": verify}))

    await transport.close()


@agent_app.command("scan")
def agent_scan(
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    output_file: str = typer.Option("", "-o", "--output", help="Save recoverable data to file (bad sectors filled with 0xFF)"),
    output: str = typer.Option("human", "--output-mode", help="Output mode: human, json"),
) -> None:
    """Scan flash health sector-by-sector (flash doctor)."""
    import asyncio
    asyncio.run(_agent_scan_async(port, output_file, output))


async def _agent_scan_async(port: str, output_file: str, output: str) -> None:
    import json as json_mod
    import time

    from rich.console import Console
    from rich.live import Live
    from rich.text import Text

    from defib.agent.client import (
        FlashAgentClient,
        ScanResult,
        SectorResult,
        SectorStatus,
    )
    from defib.transport.serial import SerialTransport

    console = Console()
    transport = await SerialTransport.create(port)
    client = FlashAgentClient(transport)

    if not await client.connect(timeout=5.0):
        console.print("[red]Agent not responding[/red]")
        await transport.close()
        raise typer.Exit(1)

    info = await client.get_info()
    flash_size = int(info.get("flash_size", 0))
    sector_size = int(info.get("sector_size", 0x10000))
    num_sectors = flash_size // sector_size if sector_size else 0

    if num_sectors == 0:
        console.print("[red]Could not detect flash[/red]")
        await transport.close()
        raise typer.Exit(1)

    # Status display mapping: (char, rich style)
    STATUS_DISPLAY = {
        SectorStatus.GOOD:           ("=", "green"),
        SectorStatus.EMPTY:          (".", "dim"),
        SectorStatus.STUCK_ZERO:     ("X", "red bold"),
        SectorStatus.STUCK_PATTERN:  ("X", "red bold"),
        SectorStatus.UNSTABLE:       ("?", "yellow"),
        SectorStatus.READ_ERROR:     ("!", "red"),
    }
    COLS = 16  # sectors per row

    sector_chars: list[tuple[str, str]] = [(" ", "dim")] * num_sectors
    scanned_count = 0
    t0 = time.time()

    def build_map() -> Text:
        text = Text()
        text.append("Flash Doctor", style="bold")
        text.append(f" — {flash_size // 1024}KB ({num_sectors} sectors x {sector_size // 1024}KB)\n\n")
        rows = (num_sectors + COLS - 1) // COLS
        for row in range(rows):
            addr = row * COLS * sector_size
            text.append(f"  0x{addr:06X} ", style="dim")
            for col in range(COLS):
                idx = row * COLS + col
                if idx >= num_sectors:
                    break
                ch, style = sector_chars[idx]
                text.append("[", style="dim")
                text.append(ch, style=style)
                text.append("]", style="dim")
            text.append("\n")
        text.append("\n  Legend: ")
        text.append("[=]", style="green")
        text.append(" Good  ")
        text.append("[.]", style="dim")
        text.append(" Empty  ")
        text.append("[X]", style="red bold")
        text.append(" Dead  ")
        text.append("[?]", style="yellow")
        text.append(" Unstable  ")
        text.append("[!]", style="red")
        text.append(" Error\n")

        elapsed = time.time() - t0
        text.append(f"  Scanned: {scanned_count}/{num_sectors}")
        if scanned_count > 0 and scanned_count < num_sectors:
            rate = scanned_count / elapsed
            eta = (num_sectors - scanned_count) / rate if rate > 0 else 0
            text.append(f"  ({elapsed:.1f}s elapsed, ~{eta:.0f}s remaining)")
        elif scanned_count == num_sectors:
            text.append(f"  ({elapsed:.1f}s)")
        text.append("\n")
        return text

    def on_sector(result: SectorResult) -> None:
        nonlocal scanned_count
        sector_chars[result.index] = STATUS_DISPLAY.get(
            result.status, ("?", "red"))
        scanned_count = result.index + 1

    scan_result: ScanResult
    if output == "human":
        console.print()
        with Live(build_map(), console=console, refresh_per_second=8) as live:
            def on_sector_live(r: SectorResult) -> None:
                on_sector(r)
                live.update(build_map())

            scan_result = await client.scan_flash(on_sector=on_sector_live)
            live.update(build_map())  # final refresh
    else:
        scan_result = await client.scan_flash(on_sector=on_sector)

    elapsed = time.time() - t0

    if output == "human":
        console.print()
        console.print("[bold]Scan Summary[/bold]")
        console.print(f"  Total sectors:  {scan_result.total}")
        console.print(f"  [green]Good:[/green]          {len(scan_result.good)}")
        console.print(f"  [dim]Empty:[/dim]         {len(scan_result.empty)}")
        console.print(f"  [yellow]Unstable:[/yellow]      {len(scan_result.unstable)}")
        console.print(f"  [red]Bad/Dead:[/red]      {len(scan_result.bad)}")
        console.print(f"  Time:           {elapsed:.1f}s")

        if scan_result.bad:
            console.print("\n[red bold]Bad sectors:[/red bold]")
            for s in scan_result.bad:
                console.print(f"  Sector {s.index:3d}: 0x{s.address:06X} — {s.status.name}")
        if scan_result.unstable:
            console.print("\n[yellow bold]Unstable sectors:[/yellow bold]")
            for s in scan_result.unstable:
                console.print(f"  Sector {s.index:3d}: 0x{s.address:06X} — CRC 0x{s.crc32:08X}")

        if not scan_result.bad and not scan_result.unstable:
            console.print("\n[green bold]Flash is healthy![/green bold]")

    elif output == "json":
        print(json_mod.dumps({
            "flash_size": scan_result.flash_size,
            "sector_size": scan_result.sector_size,
            "total": scan_result.total,
            "good": len(scan_result.good),
            "empty": len(scan_result.empty),
            "unstable": len(scan_result.unstable),
            "bad": len(scan_result.bad),
            "elapsed_s": round(elapsed, 1),
            "sectors": [
                {"index": s.index, "address": f"0x{s.address:06X}",
                 "status": s.status.name, "crc32": f"0x{s.crc32:08X}"}
                for s in scan_result.sectors
            ],
        }))

    # Optional: dump recoverable data
    if output_file:
        if output == "human":
            console.print(f"\nDumping recoverable data to [cyan]{output_file}[/cyan]...")

        recoverable = bytearray()
        readable_sectors = [
            s for s in scan_result.sectors
            if s.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE)
        ]

        for i, sector in enumerate(scan_result.sectors):
            if sector.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE):
                flash_base = 0x14000000
                data = await client.read_memory(
                    flash_base + sector.address, sector_size, fast=True,
                )
                recoverable.extend(data)
                if output == "human":
                    done = sum(1 for s in scan_result.sectors[:i + 1]
                               if s.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE))
                    console.print(
                        f"\r  Reading sector {done}/{len(readable_sectors)}...",
                        end="",
                    )
            else:
                recoverable.extend(b"\xFF" * sector_size)

        from pathlib import Path
        Path(output_file).write_bytes(recoverable)
        if output == "human":
            console.print(f"\n[green]Saved:[/green] {output_file} ({len(recoverable)} bytes)")

    await transport.close()


def _parse_size(s: str) -> int:
    """Parse size string like '16MB', '4096', '0x1000'."""
    s = s.strip().upper()
    multipliers = {"KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            return int(s[:-len(suffix)]) * mult
    return int(s, 0)


@app.command()
def install(
    chip: str = typer.Option(..., "-c", "--chip", help="Chip model name"),
    firmware: str = typer.Option(..., "--firmware", help="OpenIPC firmware tarball (.tgz)"),
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    power_cycle: bool = typer.Option(False, "--power-cycle", help="Auto power-cycle via PoE"),
    nic: str = typer.Option("", "--nic", help="Network interface for TFTP (auto-detect if empty)"),
    host_ip: str = typer.Option("192.168.1.10", "--host-ip", help="IP to assign to host NIC for TFTP"),
    device_ip: str = typer.Option("192.168.1.20", "--device-ip", help="IP for camera in U-Boot"),
    tftp_port: int = typer.Option(69, "--tftp-port", help="TFTP server port"),
    nor_size: int = typer.Option(8, "--nor-size", help="NOR flash size in MB (8 or 16)"),
    nand: bool = typer.Option(False, "--nand", help="Use NAND flash instead of NOR"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging"),
) -> None:
    """Install a full OpenIPC firmware (U-Boot + kernel + rootfs) via UART + TFTP.

    Extracts the firmware tarball, burns U-Boot to RAM via boot ROM,
    then uses TFTP to transfer kernel and rootfs to U-Boot which
    flashes them to NOR or NAND.
    """
    import asyncio
    asyncio.run(_install_async(
        chip, firmware, port, power_cycle, nic, host_ip, device_ip,
        tftp_port, nor_size, nand, output, debug,
    ))


# 8MB NOR flash layout (matches U-Boot setnor8m / mtdpartsnor8m)
_NOR8M_LAYOUT = {
    "boot":        (0x000000, 0x40000),   # 256KB
    "env":         (0x040000, 0x10000),   # 64KB
    "kernel":      (0x050000, 0x200000),  # 2MB
    "rootfs":      (0x250000, 0x500000),  # 5120KB
}

# 16MB NOR flash layout (matches U-Boot setnor16m / mtdpartsnor16m)
_NOR16M_LAYOUT = {
    "boot":        (0x000000, 0x40000),   # 256KB
    "env":         (0x040000, 0x10000),   # 64KB
    "kernel":      (0x050000, 0x300000),  # 3MB
    "rootfs":      (0x350000, 0xA00000),  # 10240KB
}

# NAND flash layout: 1M(boot),1M(env),8M(kernel),-(ubi)
_NAND_LAYOUT = {
    "boot":        (0x000000, 0x100000),   # 1MB
    "env":         (0x100000, 0x100000),   # 1MB
    "kernel":      (0x200000, 0x800000),   # 8MB
    "rootfs":      (0xA00000, 0x7600000),  # 118MB (UBI)
}


async def _install_async(
    chip: str,
    firmware_path: str,
    port: str,
    power_cycle: bool,
    nic: str,
    host_ip: str,
    device_ip: str,
    tftp_port: int,
    nor_size: int,
    nand: bool,
    output: str,
    debug: bool,
) -> None:
    import hashlib
    import json as json_mod
    import logging
    import re as re_mod
    import tarfile
    import zlib
    from pathlib import Path

    from rich.console import Console

    from defib.flashdump import get_ram_staging_addr, send_command, tftp_to_ram
    from defib.firmware import download_firmware, get_cached_path, has_firmware
    from defib.network.ip_manager import list_interfaces, temporary_ip
    from defib.network.tftp_server import start_tftp_server
    from defib.recovery.events import LogEvent, ProgressEvent
    from defib.recovery.session import RecoverySession
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console()

    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if nand:
        layout = _NAND_LAYOUT
        flash_cmd = "nand"
        flash_label = "NAND"
    else:
        layout = _NOR16M_LAYOUT if nor_size >= 16 else _NOR8M_LAYOUT
        flash_cmd = "sf"
        flash_label = f"NOR {nor_size}MB"

    # --- Step 1: Extract firmware tarball ---
    if output == "human":
        console.print("[bold]OpenIPC Firmware Install[/bold]")
        console.print(f"  Chip:  [cyan]{chip}[/cyan]")
        console.print(f"  Port:  [cyan]{port}[/cyan]")
        console.print(f"  Flash: [cyan]{flash_label}[/cyan]")

    kernel_data: bytes | None = None
    rootfs_data: bytes | None = None
    kernel_name = ""
    rootfs_name = ""

    with tarfile.open(firmware_path, "r:gz") as tf:
        for member in tf.getmembers():
            name = member.name
            if name.endswith(".md5sum"):
                continue
            if name.startswith("uImage"):
                kernel_name = name
                f = tf.extractfile(member)
                assert f is not None
                kernel_data = f.read()
            elif name.startswith("rootfs.squashfs") or name.startswith("rootfs.ubi"):
                rootfs_name = name
                f = tf.extractfile(member)
                assert f is not None
                rootfs_data = f.read()

    if not kernel_data or not rootfs_data:
        console.print("[red]Tarball missing uImage or rootfs (squashfs/ubi)[/red]")
        raise typer.Exit(1)

    # Verify md5sums if present
    with tarfile.open(firmware_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.name.endswith(".md5sum"):
                continue
            f = tf.extractfile(member)
            assert f is not None
            expected_line = f.read().decode().strip()
            expected_md5 = expected_line.split()[0]
            base_name = member.name.removesuffix(".md5sum")
            if base_name == kernel_name:
                actual = hashlib.md5(kernel_data).hexdigest()
                if actual != expected_md5:
                    console.print(f"[red]MD5 mismatch for {kernel_name}[/red]")
                    raise typer.Exit(1)
            elif base_name == rootfs_name:
                actual = hashlib.md5(rootfs_data).hexdigest()
                if actual != expected_md5:
                    console.print(f"[red]MD5 mismatch for {rootfs_name}[/red]")
                    raise typer.Exit(1)

    k_off, k_sz = layout["kernel"]
    r_off, r_sz = layout["rootfs"]

    if len(kernel_data) > k_sz:
        console.print(f"[red]Kernel too large: {len(kernel_data)} > {k_sz}[/red]")
        raise typer.Exit(1)
    if len(rootfs_data) > r_sz:
        console.print(f"[red]Rootfs too large: {len(rootfs_data)} > {r_sz}[/red]")
        raise typer.Exit(1)

    if output == "human":
        console.print(f"  Kernel: [cyan]{kernel_name}[/cyan] ({len(kernel_data)} bytes)")
        console.print(f"  Rootfs: [cyan]{rootfs_name}[/cyan] ({len(rootfs_data)} bytes)")

    # --- Step 2: Get U-Boot ---
    if not has_firmware(chip):
        console.print(f"[red]No OpenIPC U-Boot for '{chip}'[/red]")
        raise typer.Exit(1)

    cached = get_cached_path(chip)
    if not cached:
        if output == "human":
            console.print(f"  Downloading U-Boot for [cyan]{chip}[/cyan]...")
        cached = download_firmware(chip)

    uboot_data = cached.read_bytes()
    b_off, b_sz = layout["boot"]
    env_off, env_sz = layout["env"]
    # U-Boot to flash = boot partition (pad to boot+env size so env is clean)
    uboot_flash_size = b_sz + env_sz

    if output == "human":
        console.print(f"  U-Boot: [cyan]{cached.name}[/cyan] ({len(uboot_data)} bytes)")

    # --- Step 3: Power cycle + burn U-Boot to RAM ---
    power_controller = None
    poe_port = None
    if power_cycle:
        from defib.power.routeros import RouterOSController
        try:
            power_controller = RouterOSController.from_env()
        except Exception as e:
            console.print(f"[red]Power controller error:[/red] {e}")
            raise typer.Exit(1)

        port_basename = Path(port).name
        device_label = port_basename.removeprefix("uart-") if port_basename.startswith("uart-") else port_basename
        try:
            poe_port = await power_controller.find_port_by_comment(device_label)
        except Exception as e:
            console.print(f"[red]PoE port discovery failed:[/red] {e}")
            await power_controller.close()
            raise typer.Exit(1)

        if output == "human":
            console.print(f"  PoE: [cyan]{poe_port}[/cyan]")

    session = RecoverySession(
        chip=chip, firmware_path=str(cached),
        power_controller=power_controller, poe_port=poe_port,
    )

    if output == "human":
        console.print("\n[bold yellow]Phase 1: Burning U-Boot to RAM[/bold yellow]")
        if not power_cycle:
            console.print("  [yellow]Power-cycle the camera now![/yellow]")

    transport = await create_transport(normalize_port_name(port))

    def on_log(event: LogEvent) -> None:
        if output == "human":
            style = {"error": "red", "warn": "yellow", "info": "green"}.get(event.level, "")
            console.print(f"  [{style}]{event.message}[/{style}]")

    def on_progress(event: ProgressEvent) -> None:
        if output == "human" and event.message:
            console.print(f"  {event.message}")

    result = await session.run(
        transport,
        on_progress=on_progress,
        on_log=on_log,
        send_break=True,
    )

    if not result.success:
        console.print(f"[red]Burn failed:[/red] {result.error}")
        await transport.close()
        if power_controller:
            await power_controller.close()
        raise typer.Exit(1)

    if output == "human":
        console.print(f"  [green]U-Boot loaded in {result.elapsed_ms:.0f}ms[/green]")

    # --- Step 4: U-Boot console — probe flash ---
    if output == "human":
        console.print("\n[bold yellow]Phase 2: Flash via TFTP[/bold yellow]")

    ram_addr = get_ram_staging_addr(chip)

    if nand:
        resp = await send_command(transport, "nand info", timeout=5.0, wait_for="# ")
        if "error" in resp.lower() or "no nand" in resp.lower():
            console.print(f"[red]NAND detection failed:[/red] {resp.strip()}")
            await transport.close()
            raise typer.Exit(1)
        if output == "human":
            console.print("  [green]NAND flash detected[/green]")
    else:
        resp = await send_command(transport, "sf probe 0", timeout=5.0, wait_for="# ")
        if "error" in resp.lower() or "fail" in resp.lower():
            console.print(f"[red]sf probe failed:[/red] {resp.strip()}")
            await transport.close()
            raise typer.Exit(1)
        if output == "human":
            console.print("  [green]SPI flash detected[/green]")

    # --- Step 5: Start TFTP server + configure U-Boot networking ---
    if not nic:
        interfaces = list_interfaces()
        if interfaces:
            nic = interfaces[0]
        else:
            console.print("[red]No network interfaces found. Specify --nic.[/red]")
            await transport.close()
            raise typer.Exit(1)

    if output == "human":
        console.print(f"  NIC: [cyan]{nic}[/cyan], Host IP: [cyan]{host_ip}[/cyan]")

    # TFTP files: U-Boot, kernel, rootfs
    tftp_files = {
        "u-boot.bin": uboot_data,
        kernel_name: kernel_data,
        rootfs_name: rootfs_data,
    }

    async with temporary_ip(nic, host_ip, "255.255.255.0"):
        if output == "human":
            console.print("  [green]IP assigned[/green]")

        tftp_transport, tftp_protocol = await start_tftp_server(
            files=tftp_files,
            bind_addr=host_ip,
            port=tftp_port,
            done_count=3,  # U-Boot + kernel + rootfs
        )

        if output == "human":
            console.print(f"  [green]TFTP server started on {host_ip}:{tftp_port}[/green]")

        try:
            # Configure U-Boot networking
            await send_command(transport, f"setenv ipaddr {device_ip}", timeout=3.0, wait_for="# ")
            await send_command(transport, f"setenv serverip {host_ip}", timeout=3.0, wait_for="# ")

            if output == "human":
                console.print(f"  Device IP: [cyan]{device_ip}[/cyan]")

            async def tftp_and_flash(
                name: str, tftp_name: str, orig_data: bytes,
                flash_off: int, erase_sz: int,
            ) -> None:
                """TFTP download, flash write, and CRC verify."""
                if output == "human":
                    console.print(f"\n  [bold]Flashing {name}[/bold] → 0x{flash_off:X} ({len(orig_data)} bytes)")

                try:
                    resp = await tftp_to_ram(transport, ram_addr, tftp_name, timeout=120.0)
                except RuntimeError as e:
                    console.print(f"[red]TFTP failed for {name}:[/red] {e}")
                    raise typer.Exit(1)

                # Verify TFTP transfer in RAM before writing to flash
                expected_crc = zlib.crc32(orig_data) & 0xFFFFFFFF
                resp = await send_command(
                    transport,
                    f"crc32 0x{ram_addr:x} 0x{len(orig_data):x}",
                    timeout=10.0, wait_for="# ",
                )
                m = re_mod.search(r"==>\s*([0-9a-fA-F]{8})", resp)
                if m:
                    ram_crc = int(m.group(1), 16)
                    if ram_crc != expected_crc:
                        console.print(
                            f"[red]{name} CRC mismatch after TFTP![/red] "
                            f"expected={expected_crc:08X} got={ram_crc:08X}"
                        )
                        raise typer.Exit(1)
                    if output == "human":
                        console.print(f"    TFTP CRC verified: {ram_crc:08X}")

                erase_timeout = 120.0 if nand else 60.0
                await send_command(
                    transport,
                    f"{flash_cmd} erase 0x{flash_off:x} 0x{erase_sz:x}",
                    timeout=erase_timeout, wait_for="# ",
                )
                await send_command(
                    transport,
                    f"{flash_cmd} write 0x{ram_addr:x} 0x{flash_off:x} 0x{len(orig_data):x}",
                    timeout=120.0 if nand else 60.0, wait_for="# ",
                )

                # Verify flash write by reading back and checking CRC.
                # Skip for NAND — ECC/OOB makes raw read-back differ from
                # the original data; the TFTP-to-RAM CRC above is sufficient.
                if not nand:
                    await send_command(
                        transport,
                        f"{flash_cmd} read 0x{ram_addr:x} 0x{flash_off:x} 0x{len(orig_data):x}",
                        timeout=30.0, wait_for="# ",
                    )
                    resp = await send_command(
                        transport,
                        f"crc32 0x{ram_addr:x} 0x{len(orig_data):x}",
                        timeout=10.0, wait_for="# ",
                    )
                    m = re_mod.search(r"==>\s*([0-9a-fA-F]{8})", resp)
                    if m:
                        flash_crc = int(m.group(1), 16)
                        if flash_crc != expected_crc:
                            console.print(
                                f"[red]{name} flash verify failed![/red] "
                                f"expected={expected_crc:08X} got={flash_crc:08X}"
                            )
                            raise typer.Exit(1)
                        if output == "human":
                            console.print(f"    Flash verified: {flash_crc:08X}")

                if output == "human":
                    console.print(f"  [green]{name} OK[/green]")

            await tftp_and_flash("U-Boot", "u-boot.bin", uboot_data, b_off, uboot_flash_size)
            await tftp_and_flash("kernel", kernel_name, kernel_data, k_off, k_sz)
            await tftp_and_flash("rootfs", rootfs_name, rootfs_data, r_off, r_sz)

            # Set up proper boot environment
            if nand:
                if output == "human":
                    console.print("\n  [bold]Setting boot environment[/bold] (NAND)")
                # Set mtdparts and bootcmd directly — don't rely on env macros
                # which may be wrong or missing on the target device.
                # Layout: 1M(boot),1M(env),8M(kernel),-(ubi)
                await send_command(
                    transport,
                    "setenv mtdparts hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)",
                    timeout=3.0, wait_for="# ",
                )
                await send_command(
                    transport,
                    r"setenv bootcmd nand read ${baseaddr} 0x200000 0x800000\; bootm ${baseaddr}",
                    timeout=3.0, wait_for="# ",
                )
            else:
                nor_cmd = "setnor8m" if nor_size < 16 else "setnor16m"
                if output == "human":
                    console.print(f"\n  [bold]Setting boot environment[/bold] (run {nor_cmd})")
                # setnor8m does: set mtdparts, set bootcmd, saveenv, reset
                # We do it manually to avoid the auto-reset
                mtdparts_var = f"mtdpartsnor{nor_size}m"
                await send_command(transport, f"run {mtdparts_var}", timeout=3.0, wait_for="# ")
                await send_command(
                    transport, "setenv bootcmd ${bootcmdnor}", timeout=3.0, wait_for="# ",
                )
            resp = await send_command(transport, "saveenv", timeout=10.0, wait_for="# ")
            if output == "human":
                console.print("  [green]Environment saved[/green]")

            # Reset
            if output == "human":
                console.print("\n  [bold]Resetting device...[/bold]")
            await send_command(transport, "reset", timeout=3.0)

        finally:
            tftp_transport.close()

    await transport.close()
    if power_controller:
        await power_controller.close()

    if output == "human":
        console.print("\n[green bold]Install complete![/green bold] Device is rebooting into OpenIPC.")
    elif output == "json":
        import json as json_mod
        print(json_mod.dumps({"event": "done", "success": True}))


@app.command()
def restore(
    chip: str = typer.Option(..., "-c", "--chip", help="Chip model name"),
    dump: str = typer.Option(..., "-i", "--input", help="Flash dump file or directory of mtdN files"),
    port: str = typer.Option("/dev/ttyUSB0", "-p", "--port", help="Serial port"),
    uboot: str = typer.Option("", "--uboot", help="U-Boot binary to load (auto-downloads if omitted)"),
    flash_type: str = typer.Option("auto", "--flash-type", help="Flash type: auto, nor, nand, emmc"),
    mtdparts: str = typer.Option("", "--mtdparts", help="NAND partition layout (e.g. hinand:1M(boot),4M(kernel),8M(rootfs),...)"),
    host_ip: str = typer.Option("", "--host-ip", help="Host IP for TFTP (auto-detect if empty)"),
    device_ip: str = typer.Option("", "--device-ip", help="Device IP in U-Boot"),
    nic: str = typer.Option("", "--nic", help="Network interface for TFTP"),
    power_cycle: bool = typer.Option(False, "--power-cycle", help="Auto power-cycle via PoE"),
    output: str = typer.Option("human", "--output", help="Output mode: human, json"),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging"),
) -> None:
    """Restore a flash dump to a device.

    Loads U-Boot to RAM via boot protocol, then uses TFTP to transfer
    partition data and U-Boot commands to write it to flash.

    The input can be a directory of mtdN files (from a previous dump)
    or a single binary file to write to the entire flash.

    Examples:
        defib restore -c hi3516av200 -i /path/to/dump/ -p /dev/ttyUSB0 --power-cycle
        defib restore -c hi3516ev300 -i flash.bin -p /dev/ttyUSB0 --flash-type nor
    """
    import asyncio
    asyncio.run(_restore_async(
        chip, dump, port, uboot, flash_type, mtdparts, host_ip, device_ip, nic,
        power_cycle, output, debug,
    ))


async def _restore_async(
    chip: str, dump: str, port: str, uboot_path: str, flash_type: str,
    mtdparts_arg: str, host_ip: str, device_ip: str, nic: str, power_cycle: bool,
    output: str, debug: bool,
) -> None:
    import json as json_mod
    import logging
    from pathlib import Path

    from rich.console import Console

    from defib.flashdump import send_command
    from defib.recovery.events import LogEvent, ProgressEvent
    from defib.recovery.session import RecoverySession
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console()

    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # --- Resolve input: directory of mtdN files or single binary ---
    dump_path = Path(dump)
    partitions: list[tuple[str, bytes]] = []  # (name, data)

    if dump_path.is_dir():
        # Find mtdN files sorted by number
        mtd_files = sorted(dump_path.glob("mtd*"), key=lambda p: int(''.join(c for c in p.name if c.isdigit()) or '0'))
        if not mtd_files:
            console.print(f"[red]No mtd files found in {dump}[/red]")
            raise typer.Exit(1)
        for f in mtd_files:
            partitions.append((f.name, f.read_bytes()))
    elif dump_path.is_file():
        raw = dump_path.read_bytes()
        if raw[:4] == b"---\n":
            # ipctool backup format: YAML header + null + [4-byte LE len + data]*
            import re as _re
            import struct as _struct

            null_pos = raw.index(b"\x00")
            yaml_text = raw[:null_pos].decode("utf-8", errors="replace")

            # Extract mtdparts from YAML if not provided via --mtdparts
            if not mtdparts_arg:
                # Parse partition names and sizes
                yaml_parts = _re.findall(
                    r"name: (\w+)\n\s+size: (0x[0-9a-fA-F]+)", yaml_text
                )
                if yaml_parts:
                    mtd_defs = []
                    for pname, size_hex in yaml_parts:
                        sz = int(size_hex, 16)
                        if sz % (1024 * 1024) == 0:
                            mtd_defs.append(f"{sz // (1024 * 1024)}M({pname})")
                        else:
                            mtd_defs.append(f"{sz // 1024}k({pname})")
                    # Last partition fills remainder
                    last_name = yaml_parts[-1][0]
                    mtd_defs[-1] = f"-({last_name})"
                    # Detect NAND device name from YAML
                    nand_match = _re.search(r"type: (nand|nor)", yaml_text)
                    nand_name = "hinand" if nand_match and nand_match.group(1) == "nand" else "hi_sfc"
                    mtdparts_arg = f"{nand_name}:{','.join(mtd_defs)}"
                    if output == "human":
                        console.print(f"  mtdparts from backup: [dim]{mtdparts_arg[:70]}[/dim]")

            # Extract data blocks
            pos = null_pos + 1
            block_num = 0
            while pos + 4 <= len(raw):
                block_len = _struct.unpack("<I", raw[pos:pos + 4])[0]
                pos += 4
                if pos + block_len > len(raw):
                    break
                partitions.append((f"mtd{block_num}", raw[pos:pos + block_len]))
                pos += block_len
                block_num += 1

            if output == "human":
                console.print(f"  ipctool backup: {block_num} partitions from [cyan]{dump_path.name}[/cyan]")
        else:
            partitions.append(("flash", raw))
    else:
        console.print(f"[red]Not found: {dump}[/red]")
        raise typer.Exit(1)

    total_bytes = sum(len(d) for _, d in partitions)
    if output == "human":
        console.print("[bold]Flash Restore[/bold]")
        console.print(f"  Chip: [cyan]{chip}[/cyan]")
        console.print(f"  Port: [cyan]{port}[/cyan]")
        console.print(f"  Partitions: {len(partitions)} ({total_bytes // 1024 // 1024}MB total)")
        for name, data in partitions:
            console.print(f"    {name}: {len(data) // 1024}KB")

    # --- Resolve U-Boot binary ---
    if not uboot_path:
        from defib.firmware import get_cached_path, download_firmware, has_firmware
        if has_firmware(chip):
            cached = get_cached_path(chip)
            if not cached:
                if output == "human":
                    console.print(f"  Downloading U-Boot for [cyan]{chip}[/cyan]...")
                cached = download_firmware(chip)
            uboot_path = str(cached)
        else:
            console.print(f"[red]No U-Boot for '{chip}'. Specify --uboot.[/red]")
            raise typer.Exit(1)

    if output == "human":
        console.print(f"  U-Boot: [cyan]{Path(uboot_path).name}[/cyan]")

    # --- Resolve network ---
    if not host_ip:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            host_ip = s.getsockname()[0]
        except Exception:
            host_ip = "192.168.1.10"
        finally:
            s.close()

    if not device_ip:
        # Use .55 on the same subnet
        parts = host_ip.rsplit(".", 1)
        device_ip = f"{parts[0]}.55"

    if output == "human":
        console.print(f"  Host IP: [cyan]{host_ip}[/cyan]")
        console.print(f"  Device IP: [cyan]{device_ip}[/cyan]")

    # --- Power controller ---
    power_controller = None
    poe_port = None
    if power_cycle:
        from defib.power.routeros import RouterOSController
        try:
            power_controller = RouterOSController.from_env()
        except Exception as e:
            console.print(f"[red]Power controller error:[/red] {e}")
            raise typer.Exit(1)

        port_basename = Path(port).name
        device_label = port_basename.removeprefix("uart-") if port_basename.startswith("uart-") else port_basename
        try:
            poe_port = await power_controller.find_port_by_comment(device_label)
        except Exception as e:
            console.print(f"[red]PoE port discovery failed:[/red] {e}")
            if power_controller:
                await power_controller.close()
            raise typer.Exit(1)

        if output == "human":
            console.print(f"  PoE: [cyan]{poe_port}[/cyan]")

    # --- Phase 1: Burn U-Boot to RAM ---
    if output == "human":
        console.print("\n[bold yellow]Phase 1: Loading U-Boot to RAM[/bold yellow]")

    # For restore with power-cycle: power off first (kill running OS),
    # open serial on a quiet line, then let session handle power-on.
    if power_controller and poe_port:
        import asyncio as _aio
        if output == "human":
            console.print("  Powering off...")
        power_controller._saved_poe_out[poe_port] = "forced-on"
        await power_controller.power_off(poe_port)
        await _aio.sleep(3)
        if output == "human":
            console.print("  Device off, opening serial...")

    transport = await create_transport(normalize_port_name(port))
    await transport.flush_input()

    # Don't pass power controller to session — it would call power_cycle
    # which doesn't work on an already-off port. We handle power-on below.
    session = RecoverySession(
        chip=chip, firmware_path=uboot_path,
    )

    def on_progress(e: ProgressEvent) -> None:
        if output == "human" and e.message:
            console.print(f"  {e.message}")

    def on_log(e: LogEvent) -> None:
        if output == "human" and e.level != "debug":
            console.print(f"  {e.message}")

    # Start session (frame-blast begins immediately for PRESTEP0 chips),
    # then power on so the bootrom sees 0xAA+HEAD when it starts.
    if power_controller and poe_port:
        import asyncio as _aio
        session_task = _aio.create_task(
            session.run(transport, on_progress=on_progress, on_log=on_log)
        )
        await _aio.sleep(0.5)  # let frame-blast start
        if output == "human":
            console.print("  Powering on...")
        await power_controller.power_on(poe_port)
        result = await session_task
    else:
        result = await session.run(transport, on_progress=on_progress, on_log=on_log)

    if not result.success:
        console.print(f"[red]Burn failed:[/red] {result.error}")
        await transport.close()
        if power_controller:
            await power_controller.close()
        raise typer.Exit(1)

    if output == "human":
        console.print(f"  [green]U-Boot loaded ({result.elapsed_ms:.0f}ms)[/green]")

    # --- Phase 2: Detect mode (download_process or shell) ---
    if output == "human":
        console.print("\n[bold yellow]Phase 2: Detecting U-Boot mode[/bold yellow]")

    import time as _time
    buf = bytearray()
    start = _time.monotonic()
    download_mode = False

    while _time.monotonic() - start < 15:
        await transport.write(b"\x03")
        try:
            data = await transport.read(256, timeout=0.2)
            buf.extend(data)
            text = buf.decode("ascii", errors="replace")
            if "start download process" in text:
                download_mode = True
                break
            if "hisilicon #" in text or "OpenIPC #" in text or "\n=> " in text:
                break
        except Exception:
            pass

    if download_mode:
        if output == "human":
            console.print("  [cyan]Download command mode[/cyan]")
        from defib.protocol.download_cmd import DownloadCommandClient
        client = DownloadCommandClient(transport)
        send_cmd = client.send_command

        async def _send(cmd: str, timeout: float = 60.0) -> str:
            ok, out = await send_cmd(cmd, timeout=timeout)
            if not ok and output == "human":
                console.print(f"  [yellow]Warning: {cmd} → ERROR[/yellow]")
            return out
    else:
        if output == "human":
            console.print("  [cyan]U-Boot shell mode[/cyan]")

        async def _send(cmd: str, timeout: float = 60.0) -> str:
            return await send_command(transport, cmd, timeout=timeout, wait_for="# ")

    # --- Phase 3: Detect flash type ---
    if output == "human":
        console.print("\n[bold yellow]Phase 3: Detecting flash[/bold yellow]")

    detected_flash = flash_type
    if flash_type == "auto":
        # Try NAND first, then NOR, then eMMC
        resp = await _send("nand info", timeout=10)
        if "device" in resp.lower() and "error" not in resp.lower():
            detected_flash = "nand"
        else:
            resp = await _send("sf probe 0", timeout=10)
            if "error" not in resp.lower() and "fail" not in resp.lower():
                detected_flash = "nor"
            else:
                resp = await _send("mmc dev 0", timeout=10)
                if "error" not in resp.lower():
                    detected_flash = "emmc"
                else:
                    console.print("[red]Could not detect flash type. Use --flash-type.[/red]")
                    await transport.close()
                    raise typer.Exit(1)

    if output == "human":
        console.print(f"  Flash type: [cyan]{detected_flash}[/cyan]")

    # --- Phase 4: Configure networking ---
    if output == "human":
        console.print("\n[bold yellow]Phase 4: Network setup[/bold yellow]")

    await _send(f"setenv ipaddr {device_ip}")
    await _send(f"setenv serverip {host_ip}")

    # Retry ping — PHY may need time to establish link
    import asyncio as _asyncio
    ping_ok = False
    for attempt in range(5):
        resp = await _send(f"ping {host_ip}", timeout=15)
        if "is alive" in resp:
            ping_ok = True
            if output == "human":
                console.print(f"  [green]Network OK[/green] (attempt {attempt + 1})")
            break
        if output == "human":
            console.print(f"  Ping attempt {attempt + 1}/5 failed, retrying...")
        await _asyncio.sleep(3)

    if not ping_ok:
        console.print("[red]Network not available. Check ethernet connection.[/red]")
        await transport.close()
        raise typer.Exit(1)

    # --- Phase 5: TFTP + write each partition ---
    if output == "human":
        console.print("\n[bold yellow]Phase 5: Writing flash[/bold yellow]")

    from defib.network.tftp_server import start_tftp_server

    tftp_files = {name: data for name, data in partitions}
    tftp_transport, _ = await start_tftp_server(
        files=tftp_files, bind_addr=host_ip, port=69, done_count=len(partitions),
    )

    ram_addr = 0x82000000
    offset = 0
    ubi_partmap: dict[int, tuple[str, int, int]] | None = None

    # Write boot partition (offset 0) LAST — setenv/network commands during
    # restore cause U-Boot to save env to NAND within the boot area, so
    # writing boot last overwrites any env corruption.
    write_order = list(enumerate(partitions))
    boot_parts = [(i, p) for i, p in write_order if i == 0 or p[0] in ("mtd0", "boot")]
    other_parts = [(i, p) for i, p in write_order if (i, p) not in boot_parts]
    write_order = other_parts + boot_parts

    # Pre-compute partition offsets from original sequential order
    _part_offsets: dict[int, int] = {}
    _off = 0
    for i, (_, pdata) in enumerate(partitions):
        _part_offsets[i] = _off
        _off += len(pdata)

    for part_idx, (name, data) in write_order:
        # Pad to page alignment for NAND (2KB pages)
        page = 2048 if detected_flash == "nand" else 1
        write_size = ((len(data) + page - 1) // page) * page

        # Use real partition offset from mtdparts if available, else from sequential order
        if ubi_partmap is not None and part_idx in ubi_partmap:
            _, real_offset, _ = ubi_partmap[part_idx]
            offset = real_offset
        else:
            offset = _part_offsets[part_idx]

        if output == "human":
            console.print(
                f"\n  [bold]{name}[/bold]: {len(data) // 1024}KB → "
                f"0x{offset:X}"
            )

        t0 = _time.monotonic()

        # Detect partition type before TFTP
        is_ubifs = (
            detected_flash == "nand"
            and len(data) >= 4
            and data[:4] == b"\x31\x18\x10\x06"  # UBIFS superblock
        )

        if detected_flash == "nand" and is_ubifs:
            # UBI-aware write: let UBI handle bad block mapping
            if output == "human":
                console.print("    UBI partition detected")

            # Need mtdids + mtdparts for ubi commands.
            # Parse NAND partition layout from bootargs or use default.
            if ubi_partmap is None:
                import re as _re
                mtdparts_val = None

                # Use --mtdparts if provided
                if mtdparts_arg:
                    mtdparts_val = mtdparts_arg
                else:
                    # Try bootargs (look for NAND device like hinand)
                    resp = await _send("printenv bootargs", timeout=5)
                    m = _re.search(r"mtdparts=(hinand:[\S]+)", resp)
                    if not m:
                        resp = await _send("printenv mtdparts", timeout=5)
                        m = _re.search(r"mtdparts=(hinand:[\S]+)", resp)
                    if m:
                        mtdparts_val = m.group(1)

                if not mtdparts_val:
                    if output == "human":
                        console.print("    [red]Cannot determine NAND partition layout for UBI.[/red]")
                        console.print("    [red]Use --mtdparts to specify it.[/red]")
                    continue

                nand_name = mtdparts_val.split(":")[0]
                await _send(f"setenv mtdids nand0={nand_name}")
                await _send(f"setenv mtdparts mtdparts={mtdparts_val}")
                await _send("mtdparts")
                if output == "human":
                    console.print(f"    mtdparts: {mtdparts_val[:70]}")

                # Parse partition offsets and sizes
                ubi_partmap = {}
                part_str = mtdparts_val.split(":", 1)[1]
                cur_off = 0
                for i, pdef in enumerate(part_str.split(",")):
                    pdef_s = pdef.strip()
                    # Handle "-(name)" fill-remainder syntax
                    fm = _re.match(r"-\((\w+)\)", pdef_s)
                    if fm:
                        ubi_partmap[i] = (fm.group(1), cur_off, 0)
                        continue
                    pm = _re.match(r"([\d]+[kKmM]?)\((\w+)\)", pdef_s)
                    if not pm:
                        continue
                    sz_str, pname = pm.group(1), pm.group(2)
                    if sz_str.upper().endswith("M"):
                        sz = int(sz_str[:-1]) * 1024 * 1024
                    elif sz_str.upper().endswith("K"):
                        sz = int(sz_str[:-1]) * 1024
                    else:
                        sz = int(sz_str)
                    ubi_partmap[i] = (pname, cur_off, sz)
                    cur_off += sz

            # Find the real NAND offset for this partition
            part_idx = [i for i, (n, d) in enumerate(partitions) if n == name][0]
            if part_idx in ubi_partmap:
                real_name, real_off, real_sz = ubi_partmap[part_idx]
                vol_name = real_name  # kernel mounts by name (e.g. "ubi0:rootfs")
                if real_sz == 0:
                    real_sz = 0x8000000 - real_off  # fill to end of 128MB

                # TFTP → erase → ubi part → ubi create → ubi write
                # TFTP must be FIRST — ubi operations allocate memory that
                # conflicts with TFTP's network stack on constrained U-Boot.
                resp = await _send(f"tftpboot 0x{ram_addr:x} {name}", timeout=120)
                if "unknown command" in resp.lower():
                    resp = await _send(f"tftp 0x{ram_addr:x} {name}", timeout=120)
                if "done" not in resp.lower() and "bytes transferred" not in resp.lower():
                    if output == "human":
                        console.print("    [red]TFTP failed[/red]")
                    continue
                if output == "human":
                    console.print("    TFTP OK")

                if output == "human":
                    console.print(f"    Erasing 0x{real_off:X}+0x{real_sz:X}...")
                await _send(f"nand erase 0x{real_off:x} 0x{real_sz:x}", timeout=120)
                if output == "human":
                    console.print(f"    UBI format {real_name}...")
                await _send(f"ubi part {real_name}", timeout=120)
                await _send(f"ubi create {vol_name} 0x{len(data):x}", timeout=60)
                resp = await _send(f"ubi write 0x{ram_addr:x} {vol_name} 0x{len(data):x}", timeout=300)
                if "error" in resp.lower() or "cannot" in resp.lower():
                    if output == "human":
                        console.print(f"    [red]ubi write failed: {resp.strip()[-80:]}[/red]")
            else:
                if output == "human":
                    console.print(f"    [red]Partition {name} not in mtdparts[/red]")
                continue

        else:
            # Non-UBI: TFTP first, then raw write
            resp = await _send(f"tftpboot 0x{ram_addr:x} {name}", timeout=120)
            if "unknown command" in resp.lower():
                resp = await _send(f"tftp 0x{ram_addr:x} {name}", timeout=120)
            if "done" not in resp.lower() and "bytes transferred" not in resp.lower():
                if output == "human":
                    console.print("    [red]TFTP failed[/red]")
                continue
            if output == "human":
                console.print("    TFTP OK")

            if detected_flash == "nand":
                erase_size = ((len(data) + 0x1FFFF) // 0x20000) * 0x20000
                await _send(f"nand erase 0x{offset:x} 0x{erase_size:x}", timeout=120)
                await _send(f"nand write 0x{ram_addr:x} 0x{offset:x} 0x{write_size:x}", timeout=120)
            elif detected_flash == "nor":
                erase_size = ((len(data) + 0xFFFF) // 0x10000) * 0x10000
                await _send(f"sf erase 0x{offset:x} 0x{erase_size:x}", timeout=120)
                await _send(f"sf write 0x{ram_addr:x} 0x{offset:x} 0x{len(data):x}", timeout=120)
            elif detected_flash == "emmc":
                block_off = offset // 512
                block_cnt = (len(data) + 511) // 512
                await _send(f"mmc erase 0x{block_off:x} 0x{block_cnt:x}", timeout=120)
                await _send(f"mmc write 0x{ram_addr:x} 0x{block_off:x} 0x{block_cnt:x}", timeout=120)

        elapsed = _time.monotonic() - t0
        if output == "human":
            console.print(f"    Written ({elapsed:.1f}s)")

    tftp_transport.close()

    # --- Phase 6: Reset ---
    if output == "human":
        console.print("\n  Resetting device...")
    await _send("reset", timeout=5)

    await transport.close()
    if power_controller:
        await power_controller.close()

    if output == "human":
        console.print("\n[green bold]Restore complete![/green bold] Device is rebooting.")
    elif output == "json":
        print(json_mod.dumps({"event": "done", "success": True, "partitions": len(partitions)}))


def main() -> None:
    app()
