"""End-to-end Rockchip USB recovery.

The flow this exists to serve: a board whose SPI NAND was erased or
half-written. Its boot ROM finds no valid IDB, gives up on flash and falls
into MaskROM by itself at power-up — no button, no strap, no UART. So a rig
that can only cut power can still bring the board back:

    power_cycle()  ->  wait_for_device(MASKROM)  ->  download_boot()
                   ->  write_image(...)          ->  reset()

:meth:`RockchipRecovery.download_boot` is the part that turns a MaskROM device
into one that can actually touch flash; everything after it is ordinary block
writes, because the usbplug runs Rockchip's FTL and presents SPI NAND as flat
512-byte sectors with bad blocks and ECC already handled.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from defib.recovery.events import ProgressEvent, Stage
from defib.rockusb.device import (
    DeviceMode,
    RockusbDevice,
    RockusbUsbError,
    wait_for_device,
)
from defib.rockusb.loader import LoaderBlobs
from defib.rockusb.maskrom import CODE_471, CODE_472, build_maskrom_chunks
from defib.rockusb.protocol import (
    SECTOR_SIZE,
    Opcode,
    ResetSubcode,
    split_lba_transfers,
)

logger = logging.getLogger(__name__)

#: The usbplug needs a moment after the last 472 chunk before it drops off the
#: bus and comes back as a loader-mode device.
USBPLUG_SETTLE = 1.0


def _emit(cb: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if cb is not None:
        cb(event)


class RockchipRecovery:
    """Drive one board from MaskROM through to flashed and rebooted."""

    def __init__(self, device: RockusbDevice) -> None:
        self._device = device

    # -- stage 1: get the usbplug running ---------------------------------

    async def download_boot(
        self,
        blobs: LoaderBlobs,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        reenumerate_timeout: float = 15.0,
        usb_path: str | None = None,
    ) -> RockusbDevice:
        """Upload DDR init then usbplug, and wait for the device to come back.

        Returns the *new* opened device — the old handle is stale once the
        usbplug re-enumerates, so callers must use the returned one.

        Pass ``usb_path`` to require that the device coming back is the same
        one that went away. Every Rockchip board shares a VID:PID and gets a
        fresh USB address on re-enumeration, so without it a second board
        appearing mid-upload could be adopted by this session and flashed by
        mistake.
        """
        if self._device.mode is not DeviceMode.MASKROM:
            logger.info("device already past MaskROM; skipping loader upload")
            return self._device

        for entry in blobs.ddr:
            await self._upload(
                CODE_471, entry.data, blobs.use_rc4, Stage.DDR_INIT, entry.name, on_progress
            )
            if entry.delay_ms:
                await asyncio.sleep(entry.delay_ms / 1000.0)

        for entry in blobs.usbplug:
            await self._upload(
                CODE_472, entry.data, blobs.use_rc4, Stage.USBPLUG, entry.name, on_progress
            )
            if entry.delay_ms:
                await asyncio.sleep(entry.delay_ms / 1000.0)

        self._device.close()
        await asyncio.sleep(USBPLUG_SETTLE)

        found = await wait_for_device(
            timeout=reenumerate_timeout, mode=DeviceMode.LOADER, usb_path=usb_path
        )
        device = RockusbDevice(found)
        device.open()
        self._device = device
        _emit(
            on_progress,
            ProgressEvent(Stage.USBPLUG, 1, 1, f"usbplug running: {found}"),
        )
        return device

    async def _upload(
        self,
        code: int,
        blob: bytes,
        use_rc4: bool,
        stage: Stage,
        name: str,
        on_progress: Callable[[ProgressEvent], None] | None,
    ) -> None:
        chunks = build_maskrom_chunks(blob, use_rc4=use_rc4)
        sent = 0
        for chunk in chunks:
            await asyncio.to_thread(self._device.control_write, code, chunk)
            sent += len(chunk)
            _emit(
                on_progress,
                ProgressEvent(stage, sent, len(blob), f"{name} -> {code:#06x}"),
            )
        logger.debug(
            "uploaded %s (%d bytes in %d chunks) to %#06x", name, len(blob), len(chunks), code
        )

    # -- stage 2: touch flash ---------------------------------------------

    async def write_image(
        self,
        start_lba: int,
        data: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Write ``data`` to flash starting at ``start_lba``.

        Short trailing data is zero-padded up to a sector; the usbplug has no
        concept of a partial block.
        """
        if len(data) % SECTOR_SIZE:
            data = data + bytes(SECTOR_SIZE - (len(data) % SECTOR_SIZE))

        total = len(data) // SECTOR_SIZE
        written = 0
        for lba, count in split_lba_transfers(start_lba, total):
            offset = (lba - start_lba) * SECTOR_SIZE
            await asyncio.to_thread(
                self._device.command,
                Opcode.WRITE_LBA,
                address=lba,
                count=count,
                data_out=data[offset : offset + count * SECTOR_SIZE],
            )
            written += count
            _emit(
                on_progress,
                ProgressEvent(
                    Stage.FLASH_WRITE,
                    written * SECTOR_SIZE,
                    total * SECTOR_SIZE,
                    f"lba {lba}",
                ),
            )

    async def read_image(self, start_lba: int, sectors: int) -> bytes:
        """Read ``sectors`` sectors back, for verification."""
        out = bytearray()
        for lba, count in split_lba_transfers(start_lba, sectors):
            out += await asyncio.to_thread(
                self._device.command,
                Opcode.READ_LBA,
                address=lba,
                count=count,
                read_length=count * SECTOR_SIZE,
            )
        return bytes(out)

    async def read_flash_id(self) -> bytes:
        """Flash ID bytes — a cheap "is the usbplug really alive" probe."""
        return await asyncio.to_thread(
            self._device.command, Opcode.READ_FLASH_ID, read_length=5
        )

    async def reset(self, subcode: ResetSubcode = ResetSubcode.NORMAL) -> None:
        """Reset the device.

        ``ResetSubcode.MASKROM`` comes back in MaskROM rather than booting,
        which is how you chain several flash operations without needing the
        board's power cut in between.
        """
        try:
            await asyncio.to_thread(
                self._device.command, Opcode.RESET_DEVICE, subcode=int(subcode)
            )
        except RockusbUsbError as e:
            # The device is entitled to drop off the bus before it acknowledges
            # its own reset, so a failed status read here is expected.
            logger.debug("reset ack not received (device already gone): %s", e)
