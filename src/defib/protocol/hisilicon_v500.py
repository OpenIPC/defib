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
    # GK7206 (4K-class Goke) shares the V500 UART bootrom protocol — verified on
    # hardware 2026-11-19: the ROM answered the V500 handshake with chip id
    # 0x72060110. Its boot image has a different key/aux layout from gk7205v5x0
    # (key 0x2800 vs 0x2000, boot code at file 0x6000 vs 0x7000); the layout is
    # now derived from the header (v500_image_layout), so the agent path works
    # with the board's own boot partition as the donor (there is no published
    # OpenIPC donor). The HEAD/aux send-framing for gk7206 still needs an
    # on-hardware confirm against the ROM's "Verify key" check.
    "gk7206",
])

HANDSHAKE_TIMEOUT = 20.0  # seconds

# Chip ID in the handshake reply -> V500 family member.  0x72050510 was read
# from a GK7205V510; the other two follow the same pattern and are unverified.
# The xm7205v5x0 parts are the same silicon under another name.
# 0x72060110 read from a GK7206 (XM "IPC_GK7206V11A") — verified on hardware.
V500_CHIP_IDS = {
    0x72050500: "v500",
    0x72050510: "v510",
    0x72050530: "v530",
    0x72060110: "7206",
}


def v500_member(chip: str) -> str | None:
    """``"v510"`` for ``gk7205v510`` / ``xm7205v510:anything``, else None."""
    base = chip.lower().split(":", 1)[0]
    return base[-4:] if base in V500_SOCS else None
HANDSHAKE_BURST_FRAMES = 8  # 112 B, ~10 ms at 115200 baud
HANDSHAKE_REPLY_LEN = 14
CHUNK_ACK_TIMEOUT = 4.0   # seconds
MAX_NAK_RETRIES = 10
BOOT_LOAD_ADDR = 0x41000000


# V500 boot image layout (two members decoded byte-for-byte from stock flash:
# gk7205v510 and gk7206):
#   0x0000  key/HEAD area: XIP branch, then the params + reg-init table + a
#           cleartext DDR-profile name. RSA key fields live here on signed
#           parts and are zero when unsigned; neither member carries a
#           high-entropy block, so "Verify key fail" is a plaintext-header
#           check, not a signature check.
#   0x0400  params: [0]=aux-area length, [1]=boot-code length,
#           [2]=total boot length (= code + 0x200 tail), ...,
#           [5]=entry = V500_IMAGE_BASE + boot-code file offset.
#   aux_off aux area (DDR init/training code), aux-area-length bytes.
#   boot_off boot code.
# The key-area length is NOT a constant: gk7205v5x0 carries 0x2000, gk7206
# 0x2800.  Derive the boundaries from the header instead of assuming a fixed
# key area:
#   boot_off = entry - V500_IMAGE_BASE   (0x7000 on v510, 0x6000 on gk7206)
#   aux_off  = boot_off - aux_len        (0x2000 on v510, 0x2800 on gk7206)
# Where the bootrom runs the boot code after a UART download is PER MEMBER
# (confirmed on hardware 2026-11-20):
#   gk7205v5x0: at the download address + boot_off.  A probe reported
#     pc=0x41007000 (BOOT loaded at 0x41000000), not the 0x40707000 the entry
#     field holds — the entry field's absolute value is unused there.
#   gk7206: at the entry field's ABSOLUTE value (0x40706000).  Loading BOOT at
#     0x41000000 drew "Bootrom success" but the agent never ran (the ROM jumped
#     to 0x40706000, where nothing was); loading it at the image's natural base
#     0x40700000 (= V500_IMAGE_BASE), so the entry field lands on the agent, ran
#     it.  So BOOT is loaded per member and the agent is linked at base+boot_off.
V500_IMAGE_BASE = 0x40700000
V500_AUX_AREA_LEN_OFF = 0x400
V500_BOOT_CODE_LEN_OFF = 0x404
V500_TOTAL_BOOT_LEN_OFF = 0x408
V500_ENTRY_OFF = 0x414
V500_BOOT_TAIL_LEN = 0x200
V500_BOOT_CODE_ALIGN = 0x400

# Address the bootrom loads the BOOT area to, per member.
V500_BOOT_LOAD_ADDR = {
    "v500": BOOT_LOAD_ADDR,   # 0x41000000; ROM runs boot code at +boot_off
    "v510": BOOT_LOAD_ADDR,
    "v530": BOOT_LOAD_ADDR,
    "7206": V500_IMAGE_BASE,  # 0x40700000; ROM jumps to the absolute entry field
}

# Agent link address per member = V500_BOOT_LOAD_ADDR[member] + boot_off.  The
# agent MUST be built for this (see agent/Makefile); wrap_v500_payload rejects a
# mismatch.
#   gk7205v5x0 boot_off 0x7000 -> 0x41007000
#   gk7206     boot_off 0x6000 -> 0x40706000
V500_AGENT_LOAD_ADDR = {
    "v500": 0x41007000,
    "v510": 0x41007000,
    "v530": 0x41007000,
    "7206": 0x40706000,
}

# Members OpenIPC publishes a NOR U-Boot for (usable as the agent donor via
# download_v500_donor).  gk7206 has none, so its agent path needs an explicit
# -f donor (the board's own boot partition).
V500_PUBLISHED_DONORS = frozenset({"v500", "v510", "v530"})


def v500_image_layout(image: bytes) -> tuple[int, int, int]:
    """``(aux_off, aux_len, boot_off)`` for a V500 boot image, from its header.

    The boundaries are read from the header, not assumed: the key/HEAD area is
    0x2000 bytes on gk7205v5x0 but 0x2800 on gk7206, so a fixed key-area size
    would place the aux and boot code wrongly on one of them.
    """
    if len(image) < V500_ENTRY_OFF + 4:
        raise ValueError("image is too short to be a V500 boot image")
    aux_len = struct.unpack_from("<I", image, V500_AUX_AREA_LEN_OFF)[0]
    entry = struct.unpack_from("<I", image, V500_ENTRY_OFF)[0]
    boot_off = entry - V500_IMAGE_BASE
    aux_off = boot_off - aux_len
    if (aux_len == 0 or aux_len % V500_BOOT_CODE_ALIGN
            or boot_off <= 0 or boot_off % V500_BOOT_CODE_ALIGN):
        raise ValueError(
            f"V500 header is not plausible: aux-area length 0x{aux_len:x}, "
            f"entry 0x{entry:08x} -> boot offset 0x{boot_off:x}"
        )
    if not 0 < aux_off < boot_off <= len(image):
        raise ValueError(
            f"V500 header is not plausible: aux offset 0x{aux_off:x}, boot "
            f"offset 0x{boot_off:x}, image 0x{len(image):x} bytes"
        )
    return aux_off, aux_len, boot_off


def wrap_v500_payload(donor: bytes, payload: bytes, load_addr: int,
                      boot_base: int = BOOT_LOAD_ADDR) -> bytes:
    """Put ``payload`` in the boot-code area of a V500 boot image.

    ``donor`` is a complete V500 boot image (OpenIPC's
    ``u-boot-gk7205v510-nor.bin``, or the board's own boot partition — the
    gk7206 has no published donor, so its own flash is used). Its key/HEAD
    area, params and aux (DDR init) area are kept verbatim; its boot code is
    replaced by ``payload`` and the two length fields are patched to match.
    The bootrom runs the boot code in place, so ``payload`` must be linked at
    ``boot_base`` (where the BOOT area is loaded for this member) plus the
    donor's boot-code offset; ``load_addr`` is checked against that to catch a
    mismatch before it turns into a silent hang.
    """
    aux_off, aux_len, boot_off = v500_image_layout(donor)
    if boot_base + boot_off != load_addr:
        raise ValueError(
            f"donor boot code runs at 0x{boot_base + boot_off:08x}, but the "
            f"payload is linked at 0x{load_addr:08x}"
        )
    code = bytearray(payload)
    code += b"\x00" * (-len(code) % V500_BOOT_CODE_ALIGN)
    image = bytearray(donor[:boot_off]) + code + b"\x00" * V500_BOOT_TAIL_LEN
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
        boot_base: int = BOOT_LOAD_ADDR,
    ) -> RecoveryResult:
        stages: list[Stage] = []

        # The key/HEAD and aux boundaries come from the header: the key area is
        # 0x2000 on gk7205v5x0 but 0x2800 on gk7206, so a fixed 8 KiB HEAD would
        # truncate the key area and misplace the aux on the gk7206.
        try:
            aux_off, aux_len, _boot_off = v500_image_layout(firmware)
        except ValueError as e:
            return RecoveryResult(
                success=False, stages_completed=stages,
                error=f"Not a V500 boot image: {e}",
            )

        # HEAD area: the key/HEAD block, up to the aux area.
        if not await self._send_data_to_bootrom(
            transport, firmware[0:aux_off], 0,
            Stage.HEAD_AREA, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send HEAD area",
            )
        stages.append(Stage.HEAD_AREA)

        # AUX area: DDR-init code the bootrom runs before loading the boot code.
        aux_data = firmware[aux_off:aux_off + aux_len]

        if not await self._send_data_to_bootrom(
            transport, aux_data, aux_off,
            Stage.AUX_AREA, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send AUX area",
            )
        stages.append(Stage.AUX_AREA)

        # Brief pause between AUX and BOOT
        await asyncio.sleep(0.1)

        # Full boot image, loaded at this member's base.  On gk7206 the ROM
        # jumps to the image's absolute entry field, so the base must be the
        # image's natural base (0x40700000) for the boot code to land there.
        if not await self._send_data_to_bootrom(
            transport, firmware, boot_base,
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
