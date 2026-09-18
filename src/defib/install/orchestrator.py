"""Generic OpenIPC install orchestration.

Published U-Boot variants own boot-critical hardware initialization. Defib owns
the mechanics required to make the image it flashes bootable: runtime transport
settings, detected flash layout, environment migration, and instance-specific
identity such as the factory MAC. The CLI builds an InstallRequest and calls this
module.
"""

from __future__ import annotations

from typing import NoReturn

import typer

from defib.install.firmware import load_firmware_bundle, uboot_tftp_commands
from defib.install.layout import (
    NAND_LAYOUT,
    NOR8M_LAYOUT,
    align_up,
    detect_nor_size_mb,
    erased_region_crc,
    nand_bootargs,
    nor_layout,
    nor_mtdparts,
    parse_uboot_crc32,
    select_nor_size_mb,
    set_uboot_env_verified,
    uboot_flash_command_error,
    verify_spi_environment_crc,
)
from defib.install.model import InstallRequest, resolve_install_stages


TFTP_RAM_VERIFY_RETRIES = 1


async def run_install(request: InstallRequest) -> None:
    import json as json_mod
    import logging
    import re as re_mod
    import zlib
    from pathlib import Path

    from rich.console import Console
    from rich.markup import escape

    from defib.firmware import (
        download_firmware,
        get_cached_path,
        has_firmware,
        pad_to_size,
    )
    from defib.flashdump import get_ram_staging_addr, send_command
    from defib.network.ip_manager import list_interfaces_async, temporary_ip
    from defib.network.tftp_server import DEFAULT_BLOCKSIZE, start_tftp_server
    from defib.profiles.loader import recovery_mode
    from defib.recovery.events import LogEvent, ProgressEvent
    from defib.recovery.session import RecoverySession
    from defib.transport.base import TransportError, TransportTimeout
    from defib.transport.serial_platform import create_transport, normalize_port_name
    from defib.uboot_env import parse_printenv_value, select_install_ethaddr
    from defib.vendors.registry import create_uboot_bootstrap, get_stock_uboot_target

    console = Console()

    chip = request.chip
    firmware_path = request.firmware_path
    uboot_path = request.uboot_path
    port = request.port
    power_cycle = request.power_cycle
    poe_port_override = request.poe_port_override
    nic = request.nic
    host_ip = request.host_ip
    device_ip = request.device_ip
    tftp_port = request.tftp_port
    nor_size = request.nor_size
    nand = request.nand
    wipe_env = request.wipe_env
    wipe_rootfs_data = request.wipe_rootfs_data
    final_reset = request.final_reset
    tftp_via = request.tftp_via
    output = request.output
    debug = request.debug

    stage_error: str | None = None
    try:
        stages = resolve_install_stages(
            request.stages,
            request.skip_stages,
            final_reset=final_reset,
        )
    except ValueError as exc:
        # ``fail`` is defined below; defer emission until the common CLI-safe
        # error helper is available.
        stage_error = str(exc)
        stages = ()

    def fail(message: str, exit_code: int = 1) -> NoReturn:
        """Emit one CLI-safe install error and stop."""
        if output == "json":
            print(json_mod.dumps({"event": "error", "message": message}))
        elif output == "human":
            console.print(f"[red]{escape(message)}[/red]")
        raise typer.Exit(exit_code)

    def warn(message: str) -> None:
        if output == "json":
            print(json_mod.dumps({"event": "warning", "message": message}))
        elif output == "human":
            console.print(f"[yellow]{escape(message)}[/yellow]")

    if stage_error is not None:
        fail(stage_error, exit_code=2)

    skipped_stage_set = {
        stage.strip().lower()
        for stage in request.skip_stages
        if stage.strip()
    }
    if wipe_rootfs_data and "rootfs-data" in skipped_stage_set:
        fail(
            "--wipe-rootfs-data conflicts with --skip-stage rootfs-data",
            exit_code=2,
        )
    if wipe_rootfs_data and nand:
        fail("--wipe-rootfs-data is only supported for NOR installs", exit_code=2)

    stage_set = set(stages)
    needs_tftp = bool(stage_set & {"uboot", "kernel", "rootfs"})

    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    stock_target = get_stock_uboot_target(chip)
    has_stock_uboot = stock_target is not None
    preserved_stock_env: dict[str, str] = {}

    if stock_target is None:
        try:
            mode = recovery_mode(chip)
        except ValueError as exc:
            fail(str(exc))
        if mode == "usb":
            fail(
                f"{chip} recovers over USB, and `install` does not support "
                "that yet — use `defib burn` to bring the board up."
            )

    if has_stock_uboot and not nand and "env" in stage_set and not wipe_env:
        fail(
            "The env stage for a stock U-Boot migration requires --wipe-env so "
            "the OpenIPC U-Boot can start from its compiled defaults. "
            "Defib preserves and restores the captured factory ethaddr.",
            exit_code=2,
        )

    # The NOR boot and env partitions are fixed across the standard OpenIPC
    # 8/16/32 MiB layouts. Kernel/rootfs sizing is selected after ``sf probe``
    # reports the actual flash capacity.
    if nand:
        layout = NAND_LAYOUT
        flash_cmd = "nand"
        flash_label = "NAND"
        b_off, b_sz = layout["boot"]
        _, env_sz = layout["env"]
    else:
        layout = None
        flash_cmd = "sf"
        flash_label = f"NOR {nor_size}MB (override)" if nor_size else "NOR auto-detect"
        b_off, b_sz = NOR8M_LAYOUT["boot"]
        _, env_sz = NOR8M_LAYOUT["env"]

    # --- Step 1: Extract firmware tarball ---
    if output == "human":
        console.print("[bold]OpenIPC Firmware Install[/bold]")
        if stock_target is not None:
            console.print(f"  Device: [cyan]{stock_target.display_name}[/cyan]")
            console.print(f"  Vendor: [cyan]{stock_target.vendor}[/cyan]")
            console.print(
                f"  Stock U-Boot: [cyan]{stock_target.stock_uboot_name}[/cyan]"
            )
        console.print(f"  Profile: [cyan]{chip}[/cyan]")
        console.print(f"  Port:    [cyan]{port}[/cyan]")
        console.print(f"  Flash:   [cyan]{flash_label}[/cyan]")
        if request.stages or request.skip_stages:
            console.print(f"  Stages:  [cyan]{', '.join(stages)}[/cyan]")

    try:
        firmware = load_firmware_bundle(firmware_path)
    except ValueError as exc:
        fail(str(exc))

    kernel_name = firmware.kernel_name
    kernel_data = firmware.kernel
    rootfs_name = firmware.rootfs_name
    rootfs_data = firmware.rootfs

    if nand:
        assert layout is not None
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

    # --- Step 2: Resolve U-Boot artifact ---
    if uboot_path:
        source_path = Path(uboot_path)
        if not source_path.exists():
            fail(f"U-Boot override not found: {source_path}")
        uboot_raw = source_path.read_bytes()
        uboot_source_name = source_path.name
    else:
        if not has_firmware(chip):
            fail(f"No OpenIPC U-Boot for '{chip}'")
        cached = get_cached_path(chip)
        if cached is None:
            if output == "human":
                console.print(f"  Downloading U-Boot for [cyan]{chip}[/cyan]...")
            cached = download_firmware(chip)
        uboot_raw = cached.read_bytes()
        uboot_source_name = cached.name

    # Install writes a fixed boot partition. Release/override payloads may be
    # shorter, so pad only the flash image tail with erased bytes. Vendor
    # bootstraps chainload the original raw U-Boot payload.
    try:
        uboot_data = pad_to_size(uboot_raw, b_sz)
    except ValueError as exc:
        fail(f"U-Boot artifact does not fit the boot partition: {exc}")

    # Vendor migrations erase the environment only after all firmware partitions
    # have been written and verified. Generic installs retain their historical
    # --wipe-env behavior of erasing boot+env together.
    uboot_flash_size = (
        b_sz + env_sz
        if wipe_env and "env" in stage_set and not has_stock_uboot
        else b_sz
    )

    if output == "human":
        if len(uboot_raw) == len(uboot_data):
            console.print(
                f"  U-Boot: [cyan]{uboot_source_name}[/cyan] ({len(uboot_data)} bytes)"
            )
        else:
            console.print(
                f"  U-Boot: [cyan]{uboot_source_name}[/cyan] "
                f"({len(uboot_raw)} bytes → padded to {len(uboot_data)})"
            )


    # --- Step 3: Power cycle + burn U-Boot to RAM ---
    power_controller = None
    poe_port = None
    if power_cycle:
        from defib.power.factory import power_controller_from_env
        from defib.power.routeros import RouterOSController
        try:
            power_controller = power_controller_from_env()
        except Exception as e:  # noqa: BLE001 - normalize provider setup errors for CLI
            console.print(f"[red]Power controller error:[/red] {e}")
            raise typer.Exit(1)

        if isinstance(power_controller, RouterOSController):
            if poe_port_override:
                poe_port = poe_port_override
                if output == "human":
                    console.print(f"  PoE: [cyan]{poe_port}[/cyan] (explicit)")
            else:
                port_basename = Path(port).name
                device_label = (
                    port_basename.removeprefix("uart-")
                    if port_basename.startswith("uart-")
                    else port_basename
                )
                try:
                    poe_port = await power_controller.find_port_by_comment(device_label)
                except Exception as e:  # noqa: BLE001 - provider APIs are not exception-uniform
                    console.print(f"[red]PoE port discovery failed:[/red] {e}")
                    await power_controller.close()
                    raise typer.Exit(1)

                if output == "human":
                    console.print(f"  PoE: [cyan]{poe_port}[/cyan]")
        else:
            poe_port = ""
            if output == "human":
                console.print(f"  Power: [cyan]{power_controller.name()}[/cyan]")

    # Detect rack-pod power: the pod runs the SPL/DDR/U-Boot upload
    # locally, requires exclusive UART, so we open the transport AFTER.
    from defib.power.rack import RackController
    use_rack_fastboot = (
        isinstance(power_controller, RackController)
        and not has_stock_uboot
    )

    session = None
    if not has_stock_uboot:
        # Boot-ROM recovery needs a filesystem path for RecoverySession. Registered
        # vendor-U-Boot migration targets use their bootstrap path instead.
        cached = get_cached_path(chip) if not uboot_path else Path(uboot_path)
        if cached is None:
            cached = download_firmware(chip)
        session = RecoverySession(
            chip=chip, firmware_path=str(cached),
            power_controller=power_controller, poe_port=poe_port,
        )

    if output == "human":
        if has_stock_uboot:
            phase1 = "Phase 1: Chainloading through stock U-Boot"
        else:
            phase1 = "Phase 1: Burning U-Boot to RAM"
        console.print(f"\n[bold yellow]{phase1}[/bold yellow]")
        if not power_cycle:
            console.print("  [yellow]Power-cycle the camera now![/yellow]")

    transport = None
    if not use_rack_fastboot:
        transport = await create_transport(normalize_port_name(port))

        # Vectis allows only one TCP client; share the recovery transport.
        if power_controller is not None:
            from defib.power.vectis import VectisController
            from defib.transport.rfc2217 import Rfc2217Transport
            from defib.transport.socket import SocketTransport

            if isinstance(power_controller, VectisController) and isinstance(
                transport, (Rfc2217Transport, SocketTransport)
            ):
                power_controller.attach_transport(transport)

    def on_log(event: LogEvent) -> None:
        if output == "human":
            style = {"error": "red", "warn": "yellow", "info": "green"}.get(
                event.level, ""
            )
            console.print(f"  [{style}]{event.message}[/{style}]")

    def on_progress(event: ProgressEvent) -> None:
        if output == "human" and event.message:
            console.print(f"  {event.message}")

    verify_shell_echo = False
    vendor_chainloaded = False

    if has_stock_uboot:
        assert transport is not None
        assert stock_target is not None

        if power_controller is not None:
            if output == "human":
                console.print("  Power-cycling before stock U-Boot bootstrap...")
            try:
                await power_controller.power_cycle(poe_port or "")
            except Exception as exc:  # noqa: BLE001
                # Present controller/transport failures uniformly.
                console.print(f"[red]Power cycle failed:[/red] {exc}")
                await transport.close()
                await power_controller.close()
                raise typer.Exit(1)

        bootstrap_progress = None
        bootstrap_task = None
        bootstrap_progress_started = False
        if output == "human":
            from rich.progress import (
                BarColumn,
                Progress,
                TaskProgressColumn,
                TextColumn,
                TimeElapsedColumn,
            )

            bootstrap_progress = Progress(
                TextColumn("[bold blue]Stock U-Boot bootstrap"),
                BarColumn(bar_width=30),
                TaskProgressColumn(),
                TextColumn("{task.completed}/{task.total} bytes"),
                TimeElapsedColumn(),
                console=console,
            )
            bootstrap_task = bootstrap_progress.add_task("bootstrap", total=len(uboot_raw))

        def _bootstrap_progress(event: ProgressEvent) -> None:
            nonlocal bootstrap_progress_started
            if output == "json":
                on_progress(event)
                return
            if (
                bootstrap_progress is not None
                and bootstrap_task is not None
                and event.bytes_total > 0
            ):
                if not bootstrap_progress_started:
                    bootstrap_progress.start()
                    bootstrap_progress_started = True
                bootstrap_progress.update(
                    bootstrap_task, completed=event.bytes_sent, total=event.bytes_total
                )

        bootstrap = create_uboot_bootstrap(
            stock_target.handler,
            load_address=stock_target.load_address,
            on_log=lambda message: on_log(LogEvent(level="info", message=message)),
            on_progress=_bootstrap_progress,
        )
        verify_shell_echo = bootstrap.requires_echo_verification

        try:
            bootstrap_result = await bootstrap.bootstrap(
                transport,
                uboot_raw,
                filename=Path(uboot_source_name).name,
            )
        except (TimeoutError, TransportError) as exc:
            await transport.close()
            if power_controller:
                await power_controller.close()
            fail(f"U-Boot bootstrap failed: {exc}")
        finally:
            if bootstrap_progress is not None and bootstrap_progress_started:
                bootstrap_progress.stop()

        preserved_stock_env.update(bootstrap_result.preserved_env)
        vendor_chainloaded = bootstrap_result.chainloaded
        result = bootstrap_result.recovery

    elif use_rack_fastboot:
        from defib.recovery.rack_fastboot import run_rack_fastboot
        assert isinstance(power_controller, RackController)
        on_log(LogEvent(level="info", message="Pod-side fastboot in progress…"))
        result = await run_rack_fastboot(
            power_controller, chip, uboot_raw,
        )
        if result.success:
            transport = await create_transport(normalize_port_name(port))
    else:
        assert transport is not None  # opened above when not use_rack_fastboot
        assert session is not None
        result = await session.run(
            transport,
            on_progress=on_progress,
            on_log=on_log,
            send_break=False,
        )

    if not result.success:
        console.print(f"[red]Burn failed:[/red] {result.error}")
        if transport is not None:
            await transport.close()
        if power_controller:
            await power_controller.close()
        raise typer.Exit(1)
    assert transport is not None  # success ⇒ transport opened

    if vendor_chainloaded:
        partial_persistent = stage_set & {"kernel", "rootfs", "rootfs-data", "env"}
        if wipe_rootfs_data:
            partial_persistent.add("rootfs-data")
        if partial_persistent and "uboot" not in stage_set:
            await transport.close()
            if power_controller:
                await power_controller.close()
            fail(
                "A partial persistent install was requested while the device still "
                "boots genuine stock U-Boot. Include --stage uboot in this run, or "
                "run the partial stage after OpenIPC U-Boot has been installed.",
                exit_code=2,
            )
        if "uboot" in stage_set and "env" not in stage_set:
            warn(
                "U-Boot is being flashed without the env stage; the existing "
                "persistent vendor environment will be left in place."
            )

    if output == "human":
        console.print(f"  [green]U-Boot loaded in {result.elapsed_ms:.0f}ms[/green]")

    resources_closed = False

    async def close_resources() -> None:
        nonlocal resources_closed
        if resources_closed:
            return
        resources_closed = True
        await transport.close()
        if power_controller:
            await power_controller.close()

    async def close_and_fail(message: str, exit_code: int = 1) -> NoReturn:
        await close_resources()
        fail(message, exit_code)

    # --- Step 3.5: Detect U-Boot mode (download_process or shell) ---
    # Must happen here (not in session.run) to detect download_process mode.
    import asyncio as _aio
    import time as _time_mod

    buf = bytearray()
    start_detect = _time_mod.monotonic()
    download_mode = False

    while _time_mod.monotonic() - start_detect < 15:
        await transport.write(b"\x03")
        try:
            det_data = await transport.read(256, timeout=0.2)
            buf.extend(det_data)
            text = buf.decode("ascii", errors="replace")
            if "start download process" in text:
                download_mode = True
                break
            if "autoboot" in text.lower():
                if output == "human":
                    console.print("  Autoboot detected, sending Ctrl-C...")
                for _ in range(20):
                    await transport.write(b"\x03")
                    await _aio.sleep(0.1)
                break
            tail = text[-256:] if len(text) > 256 else text
            if "hisilicon #" in tail or "OpenIPC #" in tail or "\n=> " in tail:
                break
        except TransportTimeout:
            pass

    async def _wait_for_openipc_shell_after_reset(timeout: float = 20.0) -> None:
        """Interrupt the freshly flashed U-Boot and wait for its shell."""
        reset_buf = bytearray()
        start_reset = _time_mod.monotonic()
        while _time_mod.monotonic() - start_reset < timeout:
            await transport.write(b"\x03")
            try:
                chunk = await transport.read(256, timeout=0.2)
            except TransportTimeout:
                await _aio.sleep(0.05)
                continue
            if not chunk:
                await _aio.sleep(0.05)
                continue
            reset_buf.extend(chunk)
            text = reset_buf.decode("ascii", errors="replace")
            tail = text[-512:] if len(text) > 512 else text
            if "autoboot" in tail.lower():
                for _ in range(20):
                    await transport.write(b"\x03")
                    await _aio.sleep(0.05)
            if "OpenIPC #" in tail or "hisilicon #" in tail or "\n=> " in tail:
                return
        raise RuntimeError(
            "freshly flashed OpenIPC U-Boot prompt not detected after env reset"
        )

    if has_stock_uboot and download_mode:
        await close_and_fail(
            "Stock U-Boot migration requires a normal OpenIPC U-Boot shell after "
            "chainload; download_process mode is not supported for this path."
        )

    if download_mode:
        if output == "human":
            console.print("  [cyan]Download command mode detected[/cyan]")
        from defib.protocol.download_cmd import DownloadCommandClient
        dl_client = DownloadCommandClient(transport)

        async def _cmd_result(
            cmd: str,
            timeout: float = 60.0,
            *,
            allow_failure: bool = False,
            **kw: object,
        ) -> tuple[bool, str]:
            try:
                ok, out = await dl_client.send_command(cmd, timeout=timeout)
            except TransportError as exc:
                await close_and_fail(
                    f"U-Boot transport failed while running {cmd!r}: {exc}"
                )
            if not ok and not allow_failure:
                detail = out.strip()[-200:] or "<no response>"
                await close_and_fail(
                    f"U-Boot command failed or timed out while running {cmd!r}: "
                    f"{detail}"
                )
            return ok, out

        async def _cmd(cmd: str, timeout: float = 60.0, **kw: object) -> str:
            del kw
            _, out = await _cmd_result(cmd, timeout=timeout)
            return out
    else:
        if output == "human":
            console.print("  [cyan]U-Boot shell mode[/cyan]")

        async def _cmd_result(
            cmd: str,
            timeout: float = 60.0,
            *,
            allow_failure: bool = False,
            **kw: object,
        ) -> tuple[bool, str]:
            # Stock U-Boot consoles on older HiSilicon boards can corrupt bytes while
            # a line is being entered. For those boards, require U-Boot to echo
            # every character before Enter is sent. send_command also requires the
            # prompt to return, so a partial response cannot advance the install.
            for attempt in range(2):
                try:
                    out = await send_command(
                        transport,
                        cmd,
                        timeout=timeout,
                        wait_for="# ",
                        verify_echo=verify_shell_echo,
                    )
                except TransportError as exc:
                    await close_and_fail(
                        f"U-Boot transport failed while running {cmd!r}: {exc}"
                    )
                if "unknown command" not in out.lower() or attempt == 1:
                    return True, out
                await transport.write(b"\x03\r")
                await _aio.sleep(0.05)
            return True, out

        async def _cmd(cmd: str, timeout: float = 60.0, **kw: object) -> str:
            del kw
            _, out = await _cmd_result(cmd, timeout=timeout)
            return out

    async def _reset_command(timeout: float) -> None:
        """Issue reset without requiring the old U-Boot prompt to return."""
        if download_mode:
            # download_process may reset before it can emit [EOT](OK). Preserve
            # the historical behavior: once reset is issued, lack of a protocol
            # completion marker is not itself an install failure.
            await _cmd_result("reset", timeout=timeout, allow_failure=True)
            return
        try:
            await send_command(
                transport,
                "reset",
                timeout=timeout,
                wait_for=None,
                verify_echo=verify_shell_echo,
            )
        except TransportError as exc:
            await close_and_fail(
                f"U-Boot transport failed while running 'reset': {exc}"
            )

    async def _set_env_verified_or_fail(key: str, value: str) -> None:
        try:
            await set_uboot_env_verified(_cmd, key, value)
        except RuntimeError as exc:
            await close_and_fail(str(exc))

    if preserved_stock_env:
        if output == "human":
            console.print("  Applying preserved stock environment...")
        for key, value in preserved_stock_env.items():
            await _set_env_verified_or_fail(key, value)

    # Some vendor migrations need transient U-Boot settings so the chainloaded
    # OpenIPC U-Boot can perform the install itself (for example a non-default
    # PHY address needed for TFTP). These are deliberately not persistent board
    # policy: clearing the old persistent env before reboot discards them.
    if needs_tftp and stock_target is not None and stock_target.transient_env:
        if output == "human":
            console.print("  Applying transient installer environment...")
        for key, value in stock_target.transient_env:
            await _set_env_verified_or_fail(key, value)

    # --- Step 4: U-Boot console — probe flash ---
    if output == "human":
        phase2 = "Flash via TFTP" if needs_tftp else "Selected install stages"
        console.print(f"\n[bold yellow]Phase 2: {phase2}[/bold yellow]")

    ram_addr = get_ram_staging_addr(chip)

    nor_erase_block = 0x10000

    if nand:
        resp = await _cmd("nand info", timeout=5.0)
        if "error" in resp.lower() or "no nand" in resp.lower():
            await close_and_fail(f"NAND detection failed: {resp.strip()}")
        if output == "human":
            console.print("  [green]NAND flash detected[/green]")
    else:
        resp = await _cmd("sf probe 0", timeout=5.0)
        probe_error = uboot_flash_command_error(resp)
        if probe_error:
            await close_and_fail(
                f"sf probe failed ({probe_error}): {resp.strip()}"
            )
        block_match = re_mod.search(r"Block:\s*(\d+)\s*KB", resp, re_mod.IGNORECASE)
        if block_match:
            nor_erase_block = int(block_match.group(1)) * 1024

        detected_nor_size = detect_nor_size_mb(resp)
        if nor_size and detected_nor_size and nor_size != detected_nor_size:
            warn(
                f"--nor-size {nor_size} overrides detected "
                f"{detected_nor_size} MiB NOR; using the explicit override."
            )
        try:
            nor_size, nor_source = select_nor_size_mb(
                nor_size,
                detected_nor_size,
                require_detection=has_stock_uboot,
            )
        except ValueError as exc:
            await close_and_fail(str(exc), exit_code=2)
        layout = nor_layout(nor_size)

        k_off, k_sz = layout["kernel"]
        r_off, r_sz = layout["rootfs"]
        if len(kernel_data) > k_sz:
            await close_and_fail(f"Kernel too large: {len(kernel_data)} > {k_sz}")
        if len(rootfs_data) > r_sz:
            await close_and_fail(f"Rootfs too large: {len(rootfs_data)} > {r_sz}")

        persistent_nor_stages = {"uboot", "kernel", "rootfs", "rootfs-data", "env"}
        if stage_set & persistent_nor_stages or wipe_rootfs_data:
            unlock_ok, unlock_resp = await _cmd_result(
                "sf lock 0",
                timeout=5.0,
                allow_failure=True,
            )
            unlock_text = unlock_resp.lower()
            unlock_unsupported = (
                "unknown command" in unlock_text
                or "usage:" in unlock_text
                or "not supported" in unlock_text
                or "unsupported" in unlock_text
            )
            if unlock_unsupported:
                warn(
                    "U-Boot does not expose a usable `sf lock` command; "
                    "continuing and relying on erase/write result checks."
                )
            elif not unlock_ok:
                detail = unlock_resp.strip()[-200:] or "<no response>"
                await close_and_fail(
                    f"SPI NOR unlock command failed or timed out: {detail}"
                )
            else:
                unlock_error = uboot_flash_command_error(unlock_resp)
                if unlock_error:
                    await close_and_fail(
                        f"SPI NOR unlock failed ({unlock_error}): {unlock_resp.strip()}"
                    )
                if output == "human":
                    console.print("  [green]SPI NOR write protection cleared[/green]")

        if output == "human":
            console.print(
                f"  [green]SPI flash detected[/green]: {nor_size} MiB ({nor_source}), "
                f"erase block 0x{nor_erase_block:X}"
            )

    assert layout is not None
    k_off, k_sz = layout["kernel"]
    r_off, r_sz = layout["rootfs"]

    # --- Step 5: Pick a TFTP backend, stage / start, then drive U-Boot ---
    #
    # Two paths:
    #   * pod    — stage firmware bytes in the rack pod's PSRAM via
    #              RackController.tftp_put; camera fetches from the pod's
    #              W5500 IP (192.168.1.1). Zero host setup; the pod is
    #              already on the camera's local LAN.
    #   * host   — defib starts an embedded TFTP server on the host's
    #              `nic` at `host_ip`. Needs sudo / port-69 / NIC plumbing.
    #
    # `--tftp-via auto` picks pod when power=rack, host otherwise.
    use_pod_tftp = needs_tftp and (
        tftp_via == "pod"
        or (tftp_via == "auto" and isinstance(power_controller, RackController))
    )
    if (
        needs_tftp
        and tftp_via == "pod"
        and not isinstance(power_controller, RackController)
    ):
        await close_and_fail(
            "--tftp-via pod requires DEFIB_POWER_TYPE=rack "
            "(no rack pod to host TFTP)."
        )

    # Keep TFTP command lines short on old UART consoles.  ``loadaddr`` is
    # verified separately, so one-character aliases are sufficient here.
    tftp_alias = {"uboot": "u", "kernel": "k", "rootfs": "r"}
    tftp_files: dict[str, bytes] = {}
    if "uboot" in stage_set:
        tftp_files[tftp_alias["uboot"]] = uboot_data
    if "kernel" in stage_set:
        tftp_files[tftp_alias["kernel"]] = kernel_data
    if "rootfs" in stage_set:
        tftp_files[tftp_alias["rootfs"]] = rootfs_data

    # --tftp-via=auto pre-flight: if the pod doesn't have enough
    # contiguous PSRAM for the firmware, fall back to host TFTP.
    # Surfaces "too-big rootfs" cleanly instead of OOMing the staging
    # POST mid-way.  --tftp-via=pod stays strict (error on OOM, no
    # silent fallback).
    if needs_tftp and use_pod_tftp and tftp_via == "auto":
        assert isinstance(power_controller, RackController)
        total_bytes = sum(len(d) for d in tftp_files.values())
        fits, pod_stats = await power_controller.psram_can_fit(total_bytes)
        if not fits:
            _raw = pod_stats.get("psram_largest_free_block", 0)
            largest = int(_raw) if isinstance(_raw, (int, float)) else 0
            if output == "human":
                console.print(
                    f"  [yellow]Pod PSRAM has {largest // 1024} KB contiguous free, "
                    f"need {total_bytes // 1024} KB for this install — falling back "
                    f"to host TFTP.[/yellow]"
                )
            use_pod_tftp = False

    if needs_tftp and not use_pod_tftp:
        # Host TFTP needs a NIC + host_ip; pod path needs neither.
        if not nic:
            interfaces = await list_interfaces_async()
            if interfaces:
                nic = interfaces[0]
            else:
                await close_and_fail("No network interfaces found. Specify --nic.")
        if output == "human":
            console.print(f"  NIC: [cyan]{nic}[/cyan], Host IP: [cyan]{host_ip}[/cyan]")

    from contextlib import AsyncExitStack
    async with AsyncExitStack() as stack:
        # From this point onward every exit path closes the UART and power
        # controller, including validation/echo failures raised mid-install.
        stack.push_async_callback(close_resources)

        # Set up the TFTP backend.  Both branches end up with:
        #   serverip          — U-Boot's `setenv serverip` value
        #   replace_in_tftp() — async hook to swap a file mid-flow
        #                       (used by the UBI rootfs path below)
        tftp_protocol = None  # only used by host path's UBI replace
        if needs_tftp and use_pod_tftp:
            assert isinstance(power_controller, RackController)
            if output == "human":
                console.print(
                    f"  [cyan]Staging {sum(len(d) for d in tftp_files.values()) // 1024} KB "
                    f"in pod PSRAM via POST /tftp/<name>...[/cyan]"
                )
            for name, data in tftp_files.items():
                await power_controller.tftp_put(name, data, timeout=180.0)
            serverip = "192.168.1.1"
            tftp_pod = power_controller

            async def _aclose_pod_tftp() -> None:
                try:
                    await tftp_pod.tftp_clear()
                except Exception as exc:  # noqa: BLE001 - cleanup must not mask install result
                    logging.getLogger(__name__).debug("pod TFTP cleanup failed: %s", exc)

            stack.push_async_callback(_aclose_pod_tftp)

            async def replace_in_tftp(name: str, data: bytes) -> None:
                await tftp_pod.tftp_put(name, data, timeout=180.0)

            if output == "human":
                console.print(f"  [green]Pod TFTP ready on {serverip}:69[/green]")
        elif needs_tftp:
            await stack.enter_async_context(
                temporary_ip(nic, host_ip, "255.255.255.0")
            )
            if output == "human":
                console.print("  [green]IP assigned[/green]")

            tftp_transport, tftp_protocol = await start_tftp_server(
                files=tftp_files,
                bind_addr=host_ip,
                port=tftp_port,
                done_count=len(tftp_files),
            )
            stack.callback(tftp_transport.close)
            serverip = host_ip

            async def replace_in_tftp(name: str, data: bytes) -> None:
                tftp_protocol._files[name] = data

            if output == "human":
                console.print(
                    f"  [green]TFTP server started on {host_ip}:{tftp_port}[/green]"
                )

        # ── U-Boot console drive (identical for both backends, only
        #    `serverip` and `replace_in_tftp` differ) ─────────────────
        try:
            # Vendor-U-Boot migration uses short TFTP command lines because the
            # legacy UART is fragile; verify loadaddr before relying on it.  The
            # generic boot-ROM/download-mode path keeps the historical explicit
            # tftpboot address so it does not depend on printenv formatting.
            if needs_tftp:
                if has_stock_uboot:
                    await set_uboot_env_verified(_cmd, "ipaddr", device_ip)
                    await set_uboot_env_verified(_cmd, "serverip", serverip)
                    await set_uboot_env_verified(_cmd, "loadaddr", f"0x{ram_addr:x}")
                else:
                    await _cmd(f"setenv ipaddr {device_ip}", timeout=3.0)
                    await _cmd(f"setenv serverip {serverip}", timeout=3.0)

                if output == "human":
                    if has_stock_uboot:
                        console.print(
                            f"  Runtime network verified: device=[cyan]{device_ip}[/cyan], "
                            f"server=[cyan]{serverip}[/cyan], "
                            f"loadaddr=[cyan]0x{ram_addr:x}[/cyan]"
                        )
                    else:
                        console.print(
                            f"  Device IP: [cyan]{device_ip}[/cyan], "
                            f"server: [cyan]{serverip}[/cyan]"
                        )

            async def _tftp_to_ram(filename: str, timeout: float = 120.0) -> str:
                """TFTP download, preserving the generic explicit-address path."""
                tftpboot_cmd, tftp_cmd = uboot_tftp_commands(
                    filename,
                    ram_addr,
                    use_loadaddr=has_stock_uboot,
                )
                ok, resp = await _cmd_result(
                    tftpboot_cmd,
                    timeout=timeout,
                    allow_failure=True,
                )
                if "unknown command" in resp.lower():
                    ok, resp = await _cmd_result(
                        tftp_cmd,
                        timeout=timeout,
                        allow_failure=True,
                    )
                if not ok:
                    detail = resp.strip()[-200:] or "<no response>"
                    raise RuntimeError(f"TFTP command failed or timed out: {detail}")
                if "done" not in resp.lower() and "bytes transferred" not in resp.lower():
                    raise RuntimeError(f"TFTP download failed: {resp.strip()[-200:]}")
                return resp

            async def _verify_tftp_ram(
                name: str,
                tftp_name: str,
                orig_data: bytes,
                expected_crc: int,
            ) -> int:
                """Verify TFTP staging before any persistent write."""
                attempts = TFTP_RAM_VERIFY_RETRIES + 1
                for attempt in range(attempts):
                    failure: str
                    try:
                        tftp_resp = await _tftp_to_ram(tftp_name, timeout=120.0)
                    except RuntimeError as exc:
                        failure = str(exc)
                    else:
                        size_match = re_mod.search(
                            r"bytes transferred\s*=\s*(\d+)",
                            tftp_resp,
                            re_mod.IGNORECASE,
                        )
                        reported_size = (
                            int(size_match.group(1))
                            if size_match is not None
                            else None
                        )
                        if (
                            reported_size is not None
                            and reported_size != len(orig_data)
                        ):
                            failure = (
                                f"reported {reported_size} bytes, "
                                f"expected {len(orig_data)}"
                            )
                        else:
                            crc_resp = await _cmd(
                                f"crc32 0x{ram_addr:x} 0x{len(orig_data):x}",
                                timeout=10.0,
                            )
                            ram_crc = parse_uboot_crc32(crc_resp)
                            if ram_crc is None:
                                failure = (
                                    "CRC response did not contain a complete "
                                    f"checksum: {crc_resp.strip()[-120:]}"
                                )
                            elif ram_crc == expected_crc:
                                return ram_crc
                            else:
                                failure = (
                                    f"CRC expected={expected_crc:08X} "
                                    f"got={ram_crc:08X}"
                                )

                    if attempt < TFTP_RAM_VERIFY_RETRIES:
                        next_attempt = attempt + 2
                        if tftp_protocol is not None:
                            tftp_protocol.set_max_blocksize(DEFAULT_BLOCKSIZE)
                            warn(
                                f"Attempt {next_attempt}: fetching TFTP file "
                                f"{tftp_name!r} again for {name} after RAM "
                                f"verification failed ({failure}); using "
                                f"{DEFAULT_BLOCKSIZE}-byte blocks."
                            )
                        else:
                            warn(
                                f"Attempt {next_attempt}: fetching TFTP file "
                                f"{tftp_name!r} again for {name} after RAM "
                                f"verification failed ({failure})."
                            )
                        continue

                    console.print(
                        f"[red]{name} TFTP RAM verification failed after "
                        f"{attempts} attempt(s):[/red] {failure}"
                    )
                    raise typer.Exit(1)

                raise AssertionError("unreachable TFTP verification state")

            async def tftp_and_flash(
                name: str, tftp_name: str, orig_data: bytes,
                flash_off: int, erase_sz: int,
            ) -> None:
                """TFTP download, full-partition erase/write, and CRC verify."""
                if output == "human":
                    console.print(
                        f"\n  [bold]Flashing {name}[/bold] → 0x{flash_off:X} "
                        f"({len(orig_data)} bytes)"
                    )

                # Verify TFTP transfer in RAM before writing to flash. A completed
                # transfer with bad RAM contents gets one conservative retry.
                expected_crc = zlib.crc32(orig_data) & 0xFFFFFFFF
                ram_crc = await _verify_tftp_ram(
                    name,
                    tftp_name,
                    orig_data,
                    expected_crc,
                )
                if output == "human":
                    console.print(f"    TFTP CRC verified: {ram_crc:08X}")

                # OpenIPC NOR layouts define fixed kernel/rootfs partitions.
                # Erase the whole partition so no stock filesystem tail survives
                # a migration. NAND already uses partition-sized erase values.
                erase_len = erase_sz

                erase_cmd = f"{flash_cmd} erase 0x{flash_off:x} 0x{erase_len:x}"
                if output == "human":
                    console.print(
                        f"    Erase: 0x{flash_off:X}+0x{erase_len:X} "
                        f"(payload 0x{len(orig_data):X})"
                    )
                erase_resp = await _cmd(
                    erase_cmd,
                    timeout=180.0 if not nand else 120.0,
                )
                erase_error = uboot_flash_command_error(erase_resp)
                if erase_error:
                    console.print(
                        f"[red]{name} erase failed ({erase_error}):[/red]\n"
                        f"{erase_resp.strip()}"
                    )
                    raise typer.Exit(1)

                # NAND requires page-aligned write sizes (2KB pages). NOR writes
                # use the exact payload length; the HiSilicon sf implementation
                # handles a final partial erase-block write.
                write_sz = len(orig_data)
                if nand:
                    write_sz = align_up(write_sz, 2048)
                write_cmd = (
                    f"{flash_cmd} write 0x{ram_addr:x} 0x{flash_off:x} 0x{write_sz:x}"
                )
                write_resp = await _cmd(
                    write_cmd,
                    timeout=180.0 if not nand else 120.0,
                )
                write_error = uboot_flash_command_error(write_resp)
                if write_error:
                    console.print(
                        f"[red]{name} write failed ({write_error}):[/red]\n"
                        f"{write_resp.strip()}"
                    )
                    raise typer.Exit(1)

                # Verify flash write by reading back and checking CRC.
                # Skip for NAND — ECC/OOB makes raw read-back differ from
                # the original data; the TFTP-to-RAM CRC above is sufficient.
                if not nand:
                    read_resp = await _cmd(
                        f"{flash_cmd} read 0x{ram_addr:x} 0x{flash_off:x} 0x{len(orig_data):x}",
                        timeout=60.0,
                    )
                    read_error = uboot_flash_command_error(read_resp)
                    if read_error:
                        console.print(
                            f"[red]{name} readback failed ({read_error}):[/red]\n"
                            f"{read_resp.strip()}"
                        )
                        raise typer.Exit(1)
                    resp = await _cmd(
                        f"crc32 0x{ram_addr:x} 0x{len(orig_data):x}",
                        timeout=10.0,
                    )
                    flash_crc = parse_uboot_crc32(resp)
                    if flash_crc is None:
                        console.print(
                            f"[red]{name} flash readback returned no checksum:[/red] "
                            f"{resp.strip()[-200:]}"
                        )
                        raise typer.Exit(1)
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

            if "uboot" in stage_set:
                await tftp_and_flash(
                    "U-Boot", tftp_alias["uboot"], uboot_data, b_off, uboot_flash_size
                )
            if "kernel" in stage_set:
                await tftp_and_flash(
                    "kernel", tftp_alias["kernel"], kernel_data, k_off, k_sz
                )

            # For NAND, raw UBI images must be written through UBI rather than
            # ``nand write`` because bad-block skipping would shift UBIFS data.
            from defib.ubi import extract_ubifs, is_ubi_image

            if "rootfs" in stage_set and nand and is_ubi_image(rootfs_data):
                if output == "human":
                    console.print(
                        f"\n  [bold]Flashing rootfs (UBI)[/bold] → 0x{r_off:X}"
                        f" ({len(rootfs_data)} bytes)"
                    )

                ubifs_data = extract_ubifs(rootfs_data)
                if output == "human":
                    console.print(
                        f"    Extracted UBIFS: {len(ubifs_data)} bytes"
                        f" from {len(rootfs_data)} byte UBI image"
                    )

                await replace_in_tftp(tftp_alias["rootfs"], ubifs_data)
                ubifs_crc = zlib.crc32(ubifs_data) & 0xFFFFFFFF
                verified_ubifs_crc = await _verify_tftp_ram(
                    "rootfs (UBI)",
                    tftp_alias["rootfs"],
                    ubifs_data,
                    ubifs_crc,
                )
                if output == "human":
                    console.print(
                        f"    TFTP CRC verified: {verified_ubifs_crc:08X}"
                    )

                await _cmd(f"nand erase 0x{r_off:x} 0x{r_sz:x}", timeout=120.0)
                nand_name = "hinand"
                await _cmd(f"setenv mtdids nand0={nand_name}", timeout=3.0)
                await _cmd(
                    f"setenv mtdparts mtdparts={nand_name}:"
                    f"1024k(boot),1024k(env),8192k(kernel),-(ubi)",
                    timeout=3.0,
                )
                await _cmd("mtdparts", timeout=3.0)
                await _cmd("ubi part ubi", timeout=120.0)
                await _cmd("ubi create rootfs", timeout=60.0)
                resp = await _cmd(
                    f"ubi write 0x{ram_addr:x} rootfs 0x{len(ubifs_data):x}",
                    timeout=300.0,
                )
                if "error" in resp.lower() or "cannot" in resp.lower():
                    console.print(f"[red]ubi write failed:[/red] {resp.strip()[-120:]}")
                    raise typer.Exit(1)
                if output == "human":
                    console.print("  [green]rootfs (UBI) OK[/green]")
            elif "rootfs" in stage_set:
                await tftp_and_flash(
                    "rootfs", tftp_alias["rootfs"], rootfs_data, r_off, r_sz
                )

            erase_rootfs_data = (
                not nand
                and (
                    wipe_rootfs_data
                    or ("rootfs-data" in stage_set and has_stock_uboot)
                )
            )
            if erase_rootfs_data:
                data_offset = r_off + r_sz
                data_size = nor_size * 1024 * 1024 - data_offset
                if data_size <= 0:
                    raise RuntimeError("standard NOR layout leaves no rootfs_data region")
                if data_offset % nor_erase_block or data_size % nor_erase_block:
                    raise RuntimeError(
                        "rootfs_data is not aligned to the detected NOR erase block"
                    )
                if output == "human":
                    console.print(
                        f"\n  [bold]Preparing rootfs_data[/bold] → "
                        f"erase 0x{data_offset:X}+0x{data_size:X}"
                    )
                erase_resp = await _cmd(
                    f"sf erase 0x{data_offset:x} 0x{data_size:x}", timeout=300.0
                )
                erase_error = uboot_flash_command_error(erase_resp)
                if erase_error:
                    raise RuntimeError(
                        f"rootfs_data erase failed ({erase_error}): "
                        f"{erase_resp.strip()}"
                    )
                verify_resp = await _cmd(
                    f"sf read 0x{ram_addr:x} 0x{data_offset:x} 0x{data_size:x}; "
                    f"crc32 0x{ram_addr:x} 0x{data_size:x}",
                    timeout=180.0,
                )
                verify_error = uboot_flash_command_error(verify_resp)
                if verify_error:
                    raise RuntimeError(
                        f"rootfs_data erase verify failed ({verify_error})"
                    )
                actual_crc = parse_uboot_crc32(verify_resp)
                if actual_crc is None:
                    raise RuntimeError("could not parse rootfs_data erased-region CRC")
                expected_crc = erased_region_crc(data_size)
                if actual_crc != expected_crc:
                    raise RuntimeError(
                        "rootfs_data erase verify failed: "
                        f"expected={expected_crc:08X} got={actual_crc:08X}"
                    )
                if output == "human":
                    console.print(
                        f"  [green]rootfs_data erased and verified "
                        f"({actual_crc:08X})[/green]"
                    )

            # Vendor-U-Boot migrations must start from the defaults compiled into
            # the freshly flashed OpenIPC U-Boot. A valid vendor/previous env can
            # otherwise shadow those defaults indefinitely. Preserve the factory
            # MAC, erase the persistent env partition, then reset: the OpenIPC SPI
            # env backend sees the erased CRC and materializes compiled defaults.
            # After that Defib re-applies only install invariants plus instance
            # identity; device policy remains the firmware/profile's responsibility.
            if "env" in stage_set and has_stock_uboot and not nand:
                pre_reset_eth_resp = await _cmd("printenv ethaddr", timeout=5.0)
                pre_reset_eth = parse_printenv_value(pre_reset_eth_resp, "ethaddr")
                preserved_eth = preserved_stock_env.get("ethaddr")
                reset_eth, _ = select_install_ethaddr(
                    pre_reset_eth, preserved_eth, allow_generate=False
                )
                if reset_eth is None:
                    console.print(
                        "[red]Factory ethaddr is unavailable; refusing to erase "
                        "the persistent U-Boot environment.[/red]"
                    )
                    raise typer.Exit(1)
                preserved_stock_env["ethaddr"] = reset_eth

                env_off, env_size = nor_layout(nor_size)["env"]
                if env_off % nor_erase_block or env_size % nor_erase_block:
                    raise RuntimeError(
                        "U-Boot environment partition is not aligned to the "
                        "detected NOR erase block"
                    )
                if output == "human":
                    console.print(
                        "\n  [bold]Clearing persistent U-Boot environment[/bold]"
                    )
                env_erase_resp = await _cmd(
                    f"sf erase 0x{env_off:x} 0x{env_size:x}", timeout=30.0
                )
                env_erase_error = uboot_flash_command_error(env_erase_resp)
                if env_erase_error:
                    raise RuntimeError(
                        f"U-Boot environment erase failed ({env_erase_error}): "
                        f"{env_erase_resp.strip()}"
                    )

                env_verify_resp = await _cmd(
                    f"sf read 0x{ram_addr:x} 0x{env_off:x} 0x{env_size:x}; "
                    f"crc32 0x{ram_addr:x} 0x{env_size:x}",
                    timeout=30.0,
                )
                env_verify_error = uboot_flash_command_error(env_verify_resp)
                if env_verify_error:
                    raise RuntimeError(
                        f"U-Boot environment erase verify failed ({env_verify_error})"
                    )
                env_crc = parse_uboot_crc32(env_verify_resp)
                if env_crc is None:
                    raise RuntimeError(
                        "could not parse U-Boot environment erased-region CRC"
                    )
                expected_env_crc = erased_region_crc(env_size)
                if env_crc != expected_env_crc:
                    raise RuntimeError(
                        "U-Boot environment erase verify failed: "
                        f"expected={expected_env_crc:08X} got={env_crc:08X}"
                    )

                await _reset_command(timeout=1.0)
                await _wait_for_openipc_shell_after_reset()
                if output == "human":
                    console.print("  [green]OpenIPC U-Boot defaults loaded[/green]")

            # Set up the persistent boot environment.
            if "env" in stage_set and nand:
                if output == "human":
                    console.print("\n  [bold]Setting boot environment[/bold] (NAND)")
                await _cmd(
                    "setenv mtdparts hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)",
                    timeout=3.0,
                )
                await _cmd(
                    r"setenv bootcmd nand read ${baseaddr} 0x200000 0x800000\; "
                    r"bootm ${baseaddr}",
                    timeout=3.0,
                )
                bootargs = nand_bootargs(rootfs_is_ubi=is_ubi_image(rootfs_data))
                await _cmd(f"setenv bootargs {bootargs}", timeout=3.0)
            elif "env" in stage_set:
                if output == "human":
                    console.print("\n  [bold]Setting boot environment[/bold]")

                # The installer chose the NOR layout and wrote kernel/rootfs at
                # those offsets, so the persistent mtdparts value must describe
                # the same layout on the very first Linux boot. This is an install
                # invariant, not camera policy.
                mtdparts = nor_mtdparts(nor_size)
                if has_stock_uboot:
                    await set_uboot_env_verified(_cmd, "mtdparts", mtdparts)
                else:
                    # Preserve the historic boot-ROM install path; only the vendor
                    # migration path gains the stronger read-back verification.
                    await _cmd(f"setenv mtdparts {mtdparts}", timeout=3.0)
                    await _cmd("setenv bootcmd ${bootcmdnor}", timeout=3.0)

            if "env" in stage_set:
                # Preserve a factory MAC captured from stock U-Boot. For normal
                # boot-ROM installs keep the existing generic rescue-MAC behavior.
                eth_resp = await _cmd("printenv ethaddr", timeout=5.0)
                current_eth = parse_printenv_value(eth_resp, "ethaddr")
                preserved_eth = preserved_stock_env.get("ethaddr")
                selected_eth, eth_source = select_install_ethaddr(
                    current_eth,
                    preserved_eth,
                    allow_generate=not has_stock_uboot,
                )
                if selected_eth is None:
                    console.print(
                        "[red]Factory ethaddr is unavailable; refusing to generate a "
                        "replacement MAC for a stock U-Boot board.[/red]"
                    )
                    raise typer.Exit(1)

                if current_eth is None or current_eth.lower() != selected_eth.lower():
                    if output == "human":
                        if eth_source == "preserved":
                            console.print(
                                f"  Restoring factory ethaddr: [cyan]{selected_eth}[/cyan]"
                            )
                        elif eth_source == "generated":
                            console.print(
                                f"  ethaddr unavailable — assigning rescue MAC "
                                f"[cyan]{selected_eth}[/cyan]"
                            )
                    if has_stock_uboot:
                        await set_uboot_env_verified(_cmd, "ethaddr", selected_eth)
                    else:
                        await _cmd(f"setenv ethaddr {selected_eth}", timeout=3.0)
                elif output == "human":
                    label = "factory" if eth_source == "preserved" else "current"
                    console.print(
                        f"  ethaddr preserved ({label}): [cyan]{selected_eth}[/cyan]"
                    )

                save_resp = await _cmd("saveenv", timeout=10.0)
                if uboot_flash_command_error(save_resp):
                    raise RuntimeError(f"saveenv failed: {save_resp.strip()}")

                if has_stock_uboot:
                    env_off, env_size = nor_layout(nor_size)["env"]
                    persisted_crc = await verify_spi_environment_crc(
                        _cmd,
                        env_off=env_off,
                        env_size=env_size,
                        ram_addr=ram_addr,
                    )

                    verify_resp = await _cmd("printenv ethaddr", timeout=5.0)
                    saved_eth = parse_printenv_value(verify_resp, "ethaddr")
                    if saved_eth is None or saved_eth.lower() != selected_eth.lower():
                        raise RuntimeError(
                            "environment verify failed for ethaddr: "
                            f"expected={selected_eth!r} got={saved_eth!r}"
                        )

                    verify_mtd_resp = await _cmd("printenv mtdparts", timeout=5.0)
                    saved_mtdparts = parse_printenv_value(verify_mtd_resp, "mtdparts")
                    expected_mtdparts = nor_mtdparts(nor_size)
                    if saved_mtdparts != expected_mtdparts:
                        raise RuntimeError(
                            "environment verify failed for mtdparts: "
                            f"expected={expected_mtdparts!r} got={saved_mtdparts!r}"
                        )
                    if output == "human":
                        console.print(
                            "  [green]Environment saved and verified "
                            f"(SPI CRC {persisted_crc:08X})[/green]"
                        )
                elif output == "human":
                    console.print("  [green]Environment saved[/green]")

            if "reset" in stage_set:
                if output == "human":
                    console.print("\n  [bold]Resetting device...[/bold]")
                await _reset_command(timeout=3.0)
            elif output == "human" and "env" in stage_set:
                console.print(
                    "\n  [yellow]Final reset skipped; device left at U-Boot prompt.[/yellow]"
                )

        except (RuntimeError, TransportError) as exc:
            fail(str(exc))
        # The AsyncExitStack handles closing the host TFTP transport and
        # clearing pod TFTP state plus UART/power resources.

    if output == "human":
        if "reset" in stage_set:
            console.print(
                "\n[green bold]Install complete![/green bold] "
                "Device is rebooting into OpenIPC."
            )
        else:
            console.print(
                "\n[green bold]Install complete![/green bold] "
                "Device is ready at the U-Boot prompt; run reset manually when ready."
            )
    elif output == "json":
        print(json_mod.dumps({"event": "done", "success": True}))
