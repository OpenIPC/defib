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
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

V500_SOCS = frozenset([
    "gk7205v500", "gk7205v510", "gk7205v530",
    "xm7205v500", "xm7205v510", "xm7205v530",
])

HANDSHAKE_TIMEOUT = 20.0  # seconds
CHUNK_ACK_TIMEOUT = 4.0   # seconds
MAX_NAK_RETRIES = 10
BOOT_LOAD_ADDR = 0x41000000


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

        while True:
            await transport.write(handshake_frame)
            try:
                response = await transport.read(14, timeout=0.1)
                if response.startswith(b"\xbd\x00") and len(response) >= 12:
                    chip_id = struct.unpack(">I", response[8:12])[0]
                    self._chip_id = chip_id
                    _emit(on_progress, ProgressEvent(
                        stage=Stage.HANDSHAKE, bytes_sent=1, bytes_total=1,
                        message=f"Detected SoC: {hex(chip_id)}",
                    ))
                    return HandshakeResult(
                        success=True,
                        chip_id=chip_id,
                        message=f"Detected SoC: {hex(chip_id)}",
                    )
            except TransportTimeout:
                continue

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
