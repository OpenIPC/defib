"""HiSilicon V500 boot recovery protocol for GK7205V500 series.

Protocol flow:
1. Host sends BD 00 FF 01 00*8 + CRC16 continuously
2. Device responds with 14B starting BD 00, chip ID at bytes 8-11 (BE)
3. Multi-area boot: HEAD area (8KB) → AUX area → full boot image to 0x41000000
4. Each data chunk gets per-chunk ACK with retransmission on NAK ('U')
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Callable

from defib.protocol.base import BootProtocol
from defib.protocol.crc import ACK_BYTE, append_crc
from defib.protocol.frames import V500_HANDSHAKE_MAGIC
from defib.protocol.registry import register
from defib.recovery.events import (
    HandshakeResult,
    ProgressEvent,
    RecoveryResult,
    Stage,
)
from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)

V500_SOCS = frozenset([
    "gk7205v500", "gk7205v510", "gk7205v530",
    "xm7205v500", "xm7205v510", "xm7205v530",
])

HANDSHAKE_TIMEOUT = 20.0  # seconds
HANDSHAKE_BURST_FRAMES = 8  # 112 B, ~10 ms at 115200 baud
HANDSHAKE_REPLY_LEN = 14
CHUNK_ACK_TIMEOUT = 4.0   # seconds
MAX_NAK_RETRIES = 10
BOOT_LOAD_ADDR = 0x41000000


# V500 boot image layout (u-boot-xmedia include/configs/xm72050500.h):
#   0x0000  key area (8 KiB; RSA key fields, zero when unsigned)
#   0x0400  params: aux-area length, boot-code length, total boot length
#           (= code + 0x200 tail), aux/boot encryption flags, boot entry
#   0x2000  aux area (DDR init/training code), aux-area-length bytes
#   then    boot code
# After a UART download the bootrom runs the boot code in place, at
# BOOT_LOAD_ADDR + its file offset; the entry field in the params is not
# used on this path (a probe placed there reported pc=0x41007000, not the
# 0x40707000 the field holds).
V500_KEY_AREA_LEN = 0x2000
V500_AUX_AREA_LEN_OFF = 0x400
V500_BOOT_CODE_LEN_OFF = 0x404
V500_TOTAL_BOOT_LEN_OFF = 0x408
V500_BOOT_TAIL_LEN = 0x200
V500_BOOT_CODE_ALIGN = 0x400
# Where agent/Makefile links the gk7205v500 agent: BOOT_LOAD_ADDR + key area
# + the 20 KiB aux area every published V500 U-Boot carries.
V500_AGENT_LOAD_ADDR = 0x41007000


def wrap_v500_payload(donor: bytes, payload: bytes, load_addr: int) -> bytes:
    """Put ``payload`` in the boot-code area of a V500 boot image.

    ``donor`` is a complete V500 U-Boot image (e.g. OpenIPC u-boot-xmedia's
    ``u-boot-gk7205v500-nor.bin``). Its key area, params and aux (DDR init)
    area are kept verbatim; its boot code is replaced by ``payload`` and the
    two length fields are patched to match. The bootrom runs the boot code
    in place, so ``payload`` must be linked at BOOT_LOAD_ADDR plus the
    donor's boot-code offset; ``load_addr`` is checked against that to catch
    a mismatch before it turns into a silent hang on the board.
    """
    if len(donor) < V500_TOTAL_BOOT_LEN_OFF + 4:
        raise ValueError("donor is too short to be a V500 boot image")
    aux_len = struct.unpack_from("<I", donor, V500_AUX_AREA_LEN_OFF)[0]
    code_pos = V500_KEY_AREA_LEN + aux_len
    if aux_len == 0 or aux_len % V500_BOOT_CODE_ALIGN or code_pos > len(donor):
        raise ValueError(f"donor aux-area length 0x{aux_len:x} is not plausible")
    if BOOT_LOAD_ADDR + code_pos != load_addr:
        raise ValueError(
            f"donor boot code runs at 0x{BOOT_LOAD_ADDR + code_pos:08x}, but the "
            f"payload is linked at 0x{load_addr:08x}"
        )
    code = bytearray(payload)
    code += b"\x00" * (-len(code) % V500_BOOT_CODE_ALIGN)
    image = bytearray(donor[:code_pos]) + code + b"\x00" * V500_BOOT_TAIL_LEN
    struct.pack_into("<I", image, V500_BOOT_CODE_LEN_OFF, len(code))
    struct.pack_into("<I", image, V500_TOTAL_BOOT_LEN_OFF, len(code) + V500_BOOT_TAIL_LEN)
    return bytes(image)


def _emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


@register
class HiSiliconV500(BootProtocol):
    """GK7205V500 series boot protocol."""

    def __init__(self) -> None:
        self._chip_id: int | None = None

    @classmethod
    def name(cls) -> str:
        return "HiSilicon V500"

    @classmethod
    def matches(cls, chip_name: str) -> bool:
        return chip_name.lower() in V500_SOCS

    async def handshake(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> HandshakeResult:
        """Send V500 handshake frame until device responds with chip ID."""
        _emit(on_progress, ProgressEvent(
            stage=Stage.HANDSHAKE, bytes_sent=0, bytes_total=1,
            message="Waiting for bootrom... power-cycle the device now!",
        ))

        handshake_frame = append_crc(
            V500_HANDSHAKE_MAGIC + b"\x00\x00\x00\x00\x00\x00\x00\x00"
        )
        # The bootrom only listens for a few tens of ms after reset before
        # it falls through to flash boot, so the line must never go idle:
        # one 14-byte frame is ~1.2 ms on the wire, and waiting 100 ms for
        # a reply after each one left the window uncovered ~99% of the time.
        burst = handshake_frame * HANDSHAKE_BURST_FRAMES

        buffer = bytearray()
        while True:
            await transport.write(burst)
            # Read only when something has arrived (bytes_waiting() is
            # non-blocking on every transport).  An unconditional timed read
            # would reprogram the serial port's timeout — a tcsetattr — on
            # every pass.  The host TX queue paces the writes; whatever is
            # still queued when the reply lands is drained below.
            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    buffer += await transport.read(waiting, timeout=0.01)
            except TransportTimeout:
                pass

            # The reply can land anywhere in the stream — after boot noise
            # from a still-running OS, or mid-way through a burst.
            idx = buffer.find(b"\xbd\x00")
            if idx != -1 and len(buffer) - idx >= HANDSHAKE_REPLY_LEN:
                chip_id = struct.unpack(">I", buffer[idx + 8:idx + 12])[0]
                self._chip_id = chip_id
                # The rest of the burst is still in flight and the bootrom
                # answers each frame: drain our TX queue, let the replies
                # settle and drop them so they are not mistaken for ACKs
                # during the HEAD stage.
                try:
                    await transport.flush_output()
                except TransportError:
                    pass  # best effort: the settle delay still covers it
                await asyncio.sleep(0.1)
                await transport.flush_input()
                _emit(on_progress, ProgressEvent(
                    stage=Stage.HANDSHAKE, bytes_sent=1, bytes_total=1,
                    message=f"Detected SoC: {hex(chip_id)}",
                ))
                return HandshakeResult(
                    success=True,
                    chip_id=chip_id,
                    message=f"Detected SoC: {hex(chip_id)}",
                )
            if idx == -1:
                # Keep a trailing 0xBD: it may be the start of a reply.
                del buffer[:max(0, len(buffer) - 1)]
            else:
                del buffer[:idx]

    async def _send_frame_wait_ack(
        self,
        transport: Transport,
        data: bytes,
        timeout: float = CHUNK_ACK_TIMEOUT,
    ) -> bool:
        """Send a frame and wait for ACK (0xAA). Retransmit on NAK ('U')."""
        await transport.write(data)
        retries = 0
        start = time.monotonic()

        while time.monotonic() - start < timeout and retries < MAX_NAK_RETRIES:
            try:
                response = await transport.read(1, timeout=timeout)
            except TransportTimeout:
                return False

            if response == ACK_BYTE:
                return True
            if response == b"U":
                # NAK — retransmit
                await transport.write(data)
                retries += 1
                continue

        return False

    async def _send_data_to_bootrom(
        self,
        transport: Transport,
        data: bytes,
        address: int,
        stage: Stage,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> bool:
        """Send data using HEAD + DATA chunks + TAIL with per-chunk ACK."""
        total = len(data)

        # HEAD frame
        head = b"\xfe\x00\xff\x01"
        head += struct.pack(">I", total)
        head += struct.pack(">I", address)
        head = append_crc(head)

        if not await self._send_frame_wait_ack(transport, head):
            return False

        # DATA frames
        idx = 0
        pos = 0
        remaining = total
        while remaining > 0:
            idx += 1
            chunk_size = min(1024, remaining)
            chunk = data[pos:pos + chunk_size]
            pos += chunk_size
            remaining -= chunk_size

            frame = b"\xda"
            frame += struct.pack("B", idx & 0xFF)
            frame += struct.pack("B", (~idx) & 0xFF)
            frame += chunk
            frame = append_crc(frame)

            _emit(on_progress, ProgressEvent(
                stage=stage, bytes_sent=pos, bytes_total=total,
            ))

            if not await self._send_frame_wait_ack(transport, frame):
                return False

        # TAIL frame
        count = ((total + 1023) // 1024) + 1
        tail = b"\xed"
        tail += struct.pack("B", count & 0xFF)
        tail += struct.pack("B", (~count) & 0xFF)
        tail = append_crc(tail)

        if not await self._send_frame_wait_ack(transport, tail):
            return False

        _emit(on_progress, ProgressEvent(
            stage=stage, bytes_sent=total, bytes_total=total,
        ))
        return True

    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RecoveryResult:
        stages: list[Stage] = []

        # HEAD area: first 8KB
        head_area_len = 8192
        head_area_data = firmware[0:head_area_len]
        head_area_addr = 0

        if not await self._send_data_to_bootrom(
            transport, head_area_data, head_area_addr,
            Stage.HEAD_AREA, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send HEAD area",
            )
        stages.append(Stage.HEAD_AREA)

        # AUX area: size read from offset 1024 (4B LE)
        aux_offset = 8192
        aux_len = struct.unpack("<I", firmware[1024:1028])[0]
        aux_data = firmware[aux_offset:aux_offset + aux_len]

        if not await self._send_data_to_bootrom(
            transport, aux_data, aux_offset,
            Stage.AUX_AREA, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send AUX area",
            )
        stages.append(Stage.AUX_AREA)

        # Brief pause between AUX and BOOT
        await asyncio.sleep(0.1)

        # Full boot image
        if not await self._send_data_to_bootrom(
            transport, firmware, BOOT_LOAD_ADDR,
            Stage.BOOT_IMAGE, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send boot image",
            )
        stages.append(Stage.BOOT_IMAGE)

        _emit(on_progress, ProgressEvent(
            stage=Stage.COMPLETE, bytes_sent=1, bytes_total=1,
            message="Recovery complete",
        ))
        stages.append(Stage.COMPLETE)
        return RecoveryResult(success=True, stages_completed=stages)
