"""Novatek boot recovery protocol: NT9856x, NT9832x, NT9853x.

Boot source UART (BS = 0xC on NT9856x and NT9853x, 4 on NT9832x), or the
mask ROM's auto-boot once flash, SD, Ethernet and USB have all failed. Two
stages over one link at 115200 8N1, in one command format:

1. The ROM takes a *write loader* (``wrloader.bin``), the SDK's loader built
   ``STORAGEEXT=Uart``, into SRAM: IS_NVT, OPEN_DEVICE, WRLOADER, WAIT_DONE.
   The loader brings up the DDR, which takes about three seconds.
2. The write loader takes the *pack* (``FW98<part>T.BIN``), an all-in-one
   image of the device tree and U-Boot, into DRAM: GET_TMP_ADDR, MEM_WRITE,
   UBOOT, WAIT_DONE. It starts U-Boot from there. Nothing is written to
   flash.

Stage 2 is the SDK's loader source (``Include/scsi_op.h``,
``Project/Model/Src/UART/uart_upgrade.c``). No SDK document describes stage 1:
it was read out of Novatek's ``FwUpgrade.exe``, and has not been run against
silicon. The loader's image names its own SRAM address, so the table below
only guards against a loader built for another die.

Both stages come from ``novatek-recovery.tar.gz`` on OpenIPC/u-boot's
``novatek`` release, one ``recovery-<soc>-<flash>/`` folder per target.

The ROM's UART need not be the console, and both stages share one transport
here: NT98533's ROM guide says UART1 while its write loader serves UART0, and
an NT9832x of chip version A serves UART1. Unchecked on silicon.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
import tarfile
import time
import zlib
from typing import Callable

from defib.protocol.base import BootProtocol, ProtocolError
from defib.protocol.registry import register
from defib.recovery.events import (
    HandshakeResult,
    ProgressEvent,
    RecoveryResult,
    Stage,
)
from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)

# SoC -> the die's TOP version word, which IS_NVT returns.
NOVATEK_DIES: dict[str, int] = {
    "nt98560": 0x70210000, "nt98561": 0x70210000,
    "nt98562": 0x70210000, "nt98566": 0x70210000,  # NA51089
    "nt98321": 0x81200000, "nt98323": 0x81200000,  # NA51068
    "nt98532": 0x55210000, "nt98533": 0x55210000,  # NS02501
}
NOVATEK_SOCS = frozenset(NOVATEK_DIES)

# TOP version word -> the SRAM the ROM loads to: FwUpgrade.exe's table, but
# for NS02501, which it predates (that die's loaders are linked at this
# address). The loader's image has to agree.
DIE_SRAM: dict[int, int] = {
    0x70210000: 0xF07C0000,
    0x81200000: 0xFE080000,
    0x55210000: 0xF07C0000,
}

OPSET = 0xC0
OP_OPEN_DEVICE = 0x01
OP_WRLOADER = 0x02
OP_MEM_WRITE = 0x04
OP_IS_NVT = 0x81
OP_WAIT_DONE = 0x82
OP_GET_TMP_ADDR = 0x85
OP_UBOOT = 0x87

CDB_SIZE = 16
CDB_STATUS = 13
# What identifies a reply as the echo of its command: all but status and sum
CDB_ECHO = 13

# The most one ROM command takes (FwUpgrade.exe), and the largest loader
LOADER_MAX = 0x10000
# A reply is the only sign of progress, and the loader sends it before it
# takes the data: small enough that one fits in the reply timeout.
PACK_CHUNK = 0x1000
# What goes to the transport at once: 64 KiB is 5.7 s at 115200, past its
# 5 s write timeout on an adapter with little buffering
WRITE_STEP = 0x400

HANDSHAKE_TIMEOUT = 60.0
HANDSHAKE_INTERVAL = 0.25
REPLY_TIMEOUT = 30.0
# FwUpgrade.exe's wait for the write loader to bring the DDR up
LOADER_START_DELAY = 3.0
# How long it may then take to answer; the quiet before each zero byte; the
# quiet that ends its reply; its answer to the command that checks the sync
LOADER_SYNC_TIMEOUT = 20.0
LOADER_SYNC_GAP = 0.02
LOADER_SYNC_SETTLE = 0.1
LOADER_SYNC_REPLY = 2.0


def cdb_sum(cdb: bytes) -> int:
    """The Novatek sum of a command: its 16-bit words plus their indices."""
    words: tuple[int, ...] = struct.unpack("<8H", cdb)
    return sum(w + i for i, w in enumerate(words)) & 0xFFFF


def build_cdb(op: int, addr: int = 0, size: int = 0) -> bytes:
    """A 16-byte command, whose sum comes to 0."""
    cdb = bytearray(struct.pack("<BBII", OPSET, op, addr, size).ljust(CDB_SIZE, b"\0"))
    struct.pack_into("<H", cdb, CDB_SIZE - 2, -cdb_sum(bytes(cdb)) & 0xFFFF)
    return bytes(cdb)


def load_recovery_set(
    archive: bytes, chip: str, flash_type: str = "nor",
) -> tuple[bytes, bytes]:
    """``(wrloader.bin, pack)`` of a chip from novatek-recovery.tar.gz."""
    folder = f"recovery-{chip.split(':', 1)[0].lower()}-{flash_type}"
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            files = {
                m.name.removeprefix("./"): m
                for m in tar.getmembers() if m.isfile()
            }

            def read(path: str) -> bytes:
                f = tar.extractfile(files[path])
                assert f is not None
                return f.read()

            wrloader = f"{folder}/wrloader.bin"
            packs = [
                n for n in files
                if n.startswith(f"{folder}/FW") and n.upper().endswith("T.BIN")
            ]
            if wrloader not in files or len(packs) != 1:
                raise ProtocolError(f"the recovery archive has no UART set {folder}")
            loader, pack = read(wrloader), read(packs[0])
    except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
        raise ProtocolError(f"not a recovery archive: {e}") from e
    if not loader or not pack:
        raise ProtocolError(f"the recovery archive's {folder} has an empty file")
    return loader, pack


def _emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


@register
class Novatek(BootProtocol):
    """Novatek mask ROM and write loader, over UART."""

    def __init__(self) -> None:
        self._chip = ""
        self._flash_type = "nor"
        self._rx = bytearray()
        self.chip_word = 0
        # The write loader is already up (a second attempt, or QEMU's
        # boot-file=): stage 1 would be a write to address 0 there.
        self.loader_running = False

    @classmethod
    def name(cls) -> str:
        return "Novatek"

    @classmethod
    def matches(cls, chip_name: str) -> bool:
        return chip_name.lower().split(":", 1)[0] in NOVATEK_SOCS

    def set_target(self, chip: str, flash_type: str = "nor") -> None:
        """Which folder of the archive this session uses."""
        self._chip = chip.lower().split(":", 1)[0]
        self._flash_type = flash_type

    async def _command(
        self,
        transport: Transport,
        op: int,
        addr: int = 0,
        size: int = 0,
        data: bytes = b"",
        reply: int = 0,
        timeout: float = REPLY_TIMEOUT,
    ) -> bytes:
        """One command, as FwUpgrade.exe runs it: command and data go out
        together, then the echo and any reply are read. The loader's own
        messages can come in between on the same UART."""
        cdb = build_cdb(op, addr, size)
        message = cdb + data
        for off in range(0, len(message), WRITE_STEP):
            await transport.write(message[off:off + WRITE_STEP])
        await transport.flush_output()
        deadline = time.monotonic() + timeout
        while True:
            at = self._rx.find(cdb[:CDB_ECHO])
            if at >= 0 and len(self._rx) >= at + CDB_SIZE + reply:
                # As FwUpgrade.exe checks: inside the echo of a misaligned
                # frame the command reads the same, and the sum does not
                if not cdb_sum(bytes(self._rx[at:at + CDB_SIZE])):
                    break
                del self._rx[:at + 1]
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                raise TransportError(f"no answer to command 0x{op:02x}")
            try:
                self._rx += await transport.read(4096, timeout=min(left, 0.5))
            except TransportTimeout:
                continue
        status = self._rx[at + CDB_STATUS]
        out = bytes(self._rx[at + CDB_SIZE:at + CDB_SIZE + reply])
        del self._rx[:at + CDB_SIZE + reply]
        if status:
            raise ProtocolError(f"command 0x{op:02x} refused: status {status}")
        return out

    async def handshake(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> HandshakeResult:
        """Ask until the ROM answers: it has to reach UART first."""
        await transport.flush_input()
        self._rx.clear()
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        word = None
        while time.monotonic() < deadline:
            try:
                got = await self._command(
                    transport, OP_IS_NVT, reply=4, timeout=HANDSHAKE_INTERVAL,
                )
            except TransportError:
                continue
            word = struct.unpack("<I", got)[0]
            break
        if word is None:
            return HandshakeResult(
                success=False, message="No answer from the Novatek ROM on UART",
            )
        self.chip_word = word
        want = NOVATEK_DIES.get(self._chip)
        if word != want:
            return HandshakeResult(
                success=False, chip_id=word,
                message=f"This is die 0x{word:08x}, not the {self._chip}'s "
                f"{'none' if want is None else f'0x{want:08x}'}: "
                "its loader would not bring the DDR up",
            )
        await self._command(transport, OP_OPEN_DEVICE)
        _emit(on_progress, ProgressEvent(
            stage=Stage.HANDSHAKE, bytes_sent=1, bytes_total=1,
            message=f"Novatek die 0x{word:08x}",
        ))
        return HandshakeResult(success=True, chip_id=word)

    async def send_loader(
        self,
        transport: Transport,
        loader: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Stage 1: the write loader to the ROM."""
        if not 8 <= len(loader) <= LOADER_MAX:
            raise ProtocolError(f"a write loader of {len(loader)} bytes")
        # Its header's second word is the SRAM base it is linked for
        base = struct.unpack_from("<I", loader, 4)[0]
        want = DIE_SRAM.get(self.chip_word)
        if want is not None and base != want:
            raise ProtocolError(
                f"the write loader is linked at 0x{base:08x}, "
                f"this die's SRAM is at 0x{want:08x}"
            )
        await self._command(transport, OP_WRLOADER, base, len(loader), loader)
        _emit(on_progress, ProgressEvent(
            stage=Stage.SPL, bytes_sent=len(loader), bytes_total=len(loader),
        ))
        await self._command(transport, OP_WAIT_DONE)
        await asyncio.sleep(LOADER_START_DELAY)

    async def sync_loader(self, transport: Transport) -> None:
        """Get the write loader answering, and in step, with zero bytes.

        It reads commands 16 bytes at a time and never looks for a start, so
        one byte missed while it came up misaligns every command after it.
        And it runs what it should refuse: a bad opset gets an error reply
        that, written back over the frame, makes its checksum good, so the
        frame is then executed, and a size in the wrong place has it taking
        everything after as data. No command can go out before the framing
        is known. An all-zero frame is harmless, op 0 of size 0, and its
        error reply shows where a frame ended.
        """
        deadline = time.monotonic() + LOADER_SYNC_TIMEOUT
        while True:
            self._rx.clear()
            while bytes(CDB_STATUS) + b"\x01" not in self._rx:
                if time.monotonic() > deadline:
                    raise TransportError("the write loader does not answer")
                # One byte, and the next only once the line is quiet: a reply
                # comes in pieces, and a byte sent into one shifts the framing
                try:
                    self._rx += await transport.read(4096, timeout=LOADER_SYNC_GAP)
                except TransportTimeout:
                    await transport.write(b"\0")
            # The frame's second reply, and any byte that was on its way
            while True:
                try:
                    await transport.read(4096, timeout=LOADER_SYNC_SETTLE)
                except TransportTimeout:
                    break
            self._rx.clear()
            try:
                await self._command(
                    transport, OP_IS_NVT, reply=4, timeout=LOADER_SYNC_REPLY,
                )
                return
            except TransportError:
                continue

    async def send_pack(
        self,
        transport: Transport,
        pack: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        """Stage 2: the pack to the write loader, which starts U-Boot."""
        await self.sync_loader(transport)
        tmp = struct.unpack("<I", await self._command(
            transport, OP_GET_TMP_ADDR, size=4, reply=4,
        ))[0]
        for off in range(0, len(pack), PACK_CHUNK):
            part = pack[off:off + PACK_CHUNK]
            await self._command(
                transport, OP_MEM_WRITE, tmp + off, len(part), part,
            )
            _emit(on_progress, ProgressEvent(
                stage=Stage.UBOOT, bytes_sent=off + len(part),
                bytes_total=len(pack),
            ))
        status = struct.unpack("<i", await self._command(
            transport, OP_UBOOT, size=4, reply=4,
        ))[0]
        if status:
            raise ProtocolError(f"the write loader refuses the pack: {status}")
        await self._command(transport, OP_WAIT_DONE)

    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RecoveryResult:
        """``firmware`` is novatek-recovery.tar.gz."""
        start = time.monotonic()
        done: list[Stage] = [Stage.HANDSHAKE]
        try:
            loader, pack = load_recovery_set(firmware, self._chip, self._flash_type)
            if not self.loader_running:
                await self.send_loader(transport, loader, on_progress)
                done.append(Stage.SPL)
            await self.send_pack(transport, pack, on_progress)
            done.append(Stage.UBOOT)
        except (ProtocolError, TransportError) as e:
            return RecoveryResult(
                success=False, stages_completed=done, error=str(e),
                elapsed_ms=(time.monotonic() - start) * 1000,
            )
        done.append(Stage.COMPLETE)
        _emit(on_progress, ProgressEvent(
            stage=Stage.COMPLETE, bytes_sent=1, bytes_total=1,
            message="U-Boot is starting",
        ))
        return RecoveryResult(
            success=True, stages_completed=done,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
