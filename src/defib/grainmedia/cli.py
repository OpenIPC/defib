"""The GM8126 side of ``dump-flash``, ``install`` and ``restore``."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from defib.grainmedia.device import GrainMediaDevice, wait_for_device
from defib.grainmedia.images import (
    FIRMWARE_STAGES,
    check_firmware,
    install_segments,
    restore_segments,
)
from defib.grainmedia.protocol import ERASE_SIZE, MAX_FLASH_SIZE, GrainMediaError
from defib.grainmedia.recovery import GrainMediaRecovery, Segment, check_segments
from defib.install.model import InstallRequest, resolve_install_stages
from defib.profiles.loader import load_profile
from defib.recovery.events import ProgressEvent


class Reporter:
    """Human lines or JSON events, as ``--output`` asks."""

    def __init__(self, output: str) -> None:
        self._output = output
        self._console = Console()
        self._stage = ""
        self._percent = -10

    def log(self, message: str) -> None:
        if self._output == "json":
            print(json.dumps({"event": "log", "message": message}), flush=True)
        elif self._output == "human":
            self._console.print(f"  {escape(message)}")

    def progress(self, event: ProgressEvent) -> None:
        if self._output == "json":
            print(json.dumps({
                "event": "progress", "stage": event.stage.value,
                "sent": event.bytes_sent, "total": event.bytes_total,
                "percent": round(event.percent, 1),
            }), flush=True)
        elif self._output == "human":
            if event.stage.value != self._stage or event.percent < self._percent:
                self._stage, self._percent = event.stage.value, -10
            if event.percent >= self._percent + 10 or event.percent >= 100:
                self._percent = int(event.percent)
                self._console.print(f"  {self._stage}: {event.percent:.0f}%")

    def done(self, message: str, **fields: object) -> None:
        if self._output == "json":
            print(json.dumps({"event": "done", "success": True, **fields}), flush=True)
        elif self._output == "human":
            self._console.print(f"\n[green bold]{escape(message)}[/green bold]")


@asynccontextmanager
async def open_target(
    chip: str, report: Reporter, *, wait: float, usb_path: str,
    power_cycle: bool, poe_port: str = "",
) -> AsyncIterator[GrainMediaRecovery]:
    """Catch the board, check what it is and set it up for reading."""
    if power_cycle:
        from defib.power.base import PowerControllerError
        from defib.power.factory import power_controller_from_env

        try:
            controller = power_controller_from_env()
            report.log(f"Power-cycling via {controller.name()}...")
            try:
                await controller.power_cycle(poe_port)
            finally:
                await controller.close()
        except PowerControllerError as e:
            raise GrainMediaError(f"power cycle failed: {e}") from e
    found = await wait_for_device(
        load_profile(chip).usb_recovery_ids, timeout=wait, usb_path=usb_path or None
    )
    report.log(f"Found {found}")
    device = GrainMediaDevice(found)
    await asyncio.to_thread(device.open)
    recovery = GrainMediaRecovery(device)
    try:
        report.log(f"Identified {await recovery.identify()}, SPI NOR")
        await recovery.set_geometry()
        yield recovery
    finally:
        recovery.close()


def parse_size(size: str) -> int:
    """``--size``: 8MB, 0x800000, 8388608; 0 when empty."""
    if not size:
        return 0
    text = size.upper()
    try:
        if text.endswith("MB"):
            value = int(text[:-2]) * 0x100000
        else:
            value = int(size, 0)
    except ValueError as e:
        raise GrainMediaError(f"invalid flash size {size!r}") from e
    if value < ERASE_SIZE or value > MAX_FLASH_SIZE or value & (value - 1):
        raise GrainMediaError(
            f"flash size {size} is not a power of two up to 16 MiB, "
            "which is all the updater can address"
        )
    return value


async def resolve_size(recovery: GrainMediaRecovery, size: int, report: Reporter) -> int:
    detected = await recovery.detect_size()
    if detected is None and not size:
        raise GrainMediaError(
            "the flash contents do not tell the chip's size (it looks erased): "
            "give it (--size on dump-flash, --nor-size on install; "
            "restore takes it from the dump)"
        )
    if size and detected and size != detected:
        raise GrainMediaError(
            f"the flash contents show a {detected:#x}-byte chip, not {size:#x}"
        )
    chosen = size or detected
    assert chosen is not None
    report.log(f"Flash: {chosen // 1024} KiB ({'given' if size else 'detected'})")
    return chosen


async def _dump_to(
    recovery: GrainMediaRecovery, target: Path, offset: int, length: int, report: Reporter
) -> int:
    """Stream to a file beside the target, and move it there when complete."""
    tmp = target.with_name(f"{target.name}.{secrets.token_hex(4)}.partial")
    try:
        with tmp.open("xb") as fh:
            written = await recovery.dump(offset, length, fh.write, report.progress)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return written


async def dump_flash(
    chip: str, output_file: str, *, size: str, partition: str, wait: float,
    usb_path: str, power_cycle: bool, output: str,
) -> None:
    report = Reporter(output)
    profile = load_profile(chip)
    extent = profile.partitions.get(partition) if partition else None
    if partition and extent is None:
        known = ", ".join(sorted(profile.partitions)) or "(none declared)"
        raise GrainMediaError(f"{chip} declares no partition {partition!r}. Known: {known}")
    given = parse_size(size)
    async with open_target(
        chip, report, wait=wait, usb_path=usb_path, power_cycle=power_cycle
    ) as recovery:
        flash_size = await resolve_size(recovery, given, report)
        offset, length = 0, flash_size
        if extent is not None:
            offset, length = extent.lba * 512, extent.size_bytes
            if offset + length > flash_size:
                raise GrainMediaError(f"{partition} runs past the {flash_size:#x}-byte flash")
        await recovery.probe_read()
        written = await _dump_to(recovery, Path(output_file), offset, length, report)
    report.done(f"Dumped {written} bytes to {output_file}", file=output_file, bytes=written)


def _reserve_backup(chip: str) -> Path:
    """Create an empty file under a name no other run has or will get.

    A second attempt must not replace the backup that still holds the factory
    contents, and neither may a run on another port in the same second.
    """
    stem = f"{chip.split(':')[0]}-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    number = 1
    while True:
        path = Path(f"{stem}.bin" if number == 1 else f"{stem}_{number}.bin")
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            number += 1
            continue
        return path


async def _backup_and_write(
    chip: str, recovery: GrainMediaRecovery, segments: list[Segment],
    flash_size: int, report: Reporter,
) -> Path:
    check_segments(segments, flash_size)
    backup = _reserve_backup(chip)
    report.log(f"Backing up the flash to {backup} (may hold Wi-Fi keys and other secrets)")
    try:
        await recovery.probe_read()
        await _dump_to(recovery, backup, 0, flash_size, report)
    except BaseException:
        backup.unlink(missing_ok=True)  # still the empty reservation
        raise
    for segment in segments:
        report.log(f"{segment.name}: {segment.offset:#x}..{segment.end:#x}")
    try:
        await recovery.write(segments, flash_size, report.progress)
    except GrainMediaError as e:
        raise GrainMediaError(
            f"{e}. The write did not finish; the flash as it was is in {backup}. "
            "Power-cycle the camera and run the same command again"
        ) from e
    return backup


async def restore(
    chip: str, dump: str, *, wait: float, usb_path: str,
    power_cycle: bool, poe_port: str, output: str,
) -> None:
    report = Reporter(output)
    source = Path(dump)
    if not source.is_file():
        raise GrainMediaError(f"{dump} is not a file: {chip} restores one whole-chip dump")
    image = source.read_bytes()
    size = parse_size(str(len(image)))
    segments = restore_segments(image, size)
    async with open_target(
        chip, report, wait=wait, usb_path=usb_path,
        power_cycle=power_cycle, poe_port=poe_port,
    ) as recovery:
        detected = await recovery.detect_size()
        if detected is not None and detected != size:
            raise GrainMediaError(f"the dump is {size:#x} bytes and the flash {detected:#x}")
        backup = await _backup_and_write(chip, recovery, segments, size, report)
    report.done("Restore complete. Power-cycle the camera.", backup=str(backup))


def _load_uboot(path: str, chip: str, report: Reporter) -> bytes:
    if path:
        return Path(path).read_bytes()
    from defib.firmware import download_firmware, get_cached_path

    cached = get_cached_path(chip, "nor")
    if cached is None:
        report.log(f"Downloading U-Boot for {chip}...")
        try:
            cached = download_firmware(chip, flash_type="nor")
        except Exception as e:
            raise GrainMediaError(
                f"cannot get U-Boot for {chip} ({e}); pass a u-boot-{chip}-nor.bin with --uboot"
            ) from e
    return cached.read_bytes()


async def install(request: InstallRequest) -> None:
    from defib.install.firmware import load_firmware_bundle

    report = Reporter(request.output)
    chip = request.chip
    if request.nand:
        raise GrainMediaError(f"{chip} install supports SPI NOR only")
    stages = set(resolve_install_stages(
        request.stages, request.skip_stages, final_reset=request.final_reset
    ))
    chosen = {s.strip().lower() for s in request.stages}
    skipped = {s.strip().lower() for s in request.skip_stages}
    for flag, wanted, stage in (
        ("--wipe-rootfs-data", request.wipe_rootfs_data, "rootfs-data"),
        ("--wipe-env", request.wipe_env, "env"),
    ):
        if wanted and (stage in skipped or (chosen and stage not in chosen)):
            raise GrainMediaError(f"{flag} needs the {stage} stage")
    profile = load_profile(chip)
    base = chip.split(":")[0].lower()
    bundle = None
    if stages & FIRMWARE_STAGES:
        bundle = load_firmware_bundle(request.firmware_path)
        check_firmware(bundle, base)
    uboot = _load_uboot(request.uboot_path, chip, report) if "uboot" in stages else None

    def plan(flash_size: int) -> list[Segment]:
        return install_segments(
            profile, stages, flash_size, bundle=bundle, uboot=uboot,
            wipe_env=request.wipe_env, wipe_rootfs_data=request.wipe_rootfs_data,
        )

    given = parse_size(f"{request.nor_size * 0x100000}") if request.nor_size else 0
    # Image problems show before the camera is touched.
    if not plan(given or MAX_FLASH_SIZE):
        raise GrainMediaError(
            "nothing to write: the env and rootfs-data stages need --wipe-env and "
            "--wipe-rootfs-data, and the updater has no reset command"
        )
    async with open_target(
        chip, report, wait=request.usb_wait, usb_path=request.usb_path,
        power_cycle=request.power_cycle, poe_port=request.poe_port_override,
    ) as recovery:
        flash_size = await resolve_size(recovery, given, report)
        backup = await _backup_and_write(chip, recovery, plan(flash_size), flash_size, report)
    report.done("Install complete. Power-cycle the camera.", backup=str(backup))
