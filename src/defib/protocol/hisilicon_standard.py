"""Standard HiSilicon boot recovery protocol.

Handles ~90 classic HiSilicon/Goke SoCs that use the init_bootmode handshake
followed by DDR step, SPL, and U-Boot transfer using HEAD/DATA/TAIL frames.

Protocol flow:
1. Device sends 0x20 bytes on UART at 115200 8N1
2. Host detects 5x 0x20, sends 0xAA → device enters boot mode
3. Transfer DDR init step (64 bytes) to SRAM
4. Transfer SPL to SRAM
5. Transfer U-Boot to DDR
"""

from __future__ import annotations

import logging
from typing import Callable

from defib.protocol.base import BootProtocol
from defib.protocol.crc import ACK_BYTE, MAX_DATA_LEN
from defib.protocol.frames import (
    DataFrame,
    HeadFrame,
    TailFrame,
    chunk_data,
)
from defib.protocol.registry import register
from defib.profiles.loader import list_chips
from defib.profiles.schema import SoCProfile
from defib.recovery.events import (
    HandshakeResult,
    ProgressEvent,
    RecoveryResult,
    Stage,
)
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

BOOTMODE_MARKER = b"\x20"
BOOTMODE_COUNT = 5
BOOTMODE_ACK = b"\xaa"
MAX_INIT_READS = 30  # Only used by tests; interactive mode loops forever

FRAME_SEND_RETRIES_SHORT = 16
FRAME_SEND_RETRIES_LONG = 32


def _emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


@register
class HiSiliconStandard(BootProtocol):
    """Standard HiSilicon boot protocol for classic SoCs."""

    def __init__(self) -> None:
        self._profile: SoCProfile | None = None
        self._continuous_ack = True

    @classmethod
    def name(cls) -> str:
        return "HiSilicon Standard"

    @classmethod
    def matches(cls, chip_name: str) -> bool:
        return chip_name.lower() in [c.lower() for c in list_chips()]

    def set_profile(self, profile: SoCProfile) -> None:
        self._profile = profile

    def set_continuous_ack(self, enabled: bool) -> None:
        """Enable continuous 0xAA sending during handshake.

        When enabled, the handshake sends 0xAA every ~50ms while waiting
        for the bootrom 0x20 pattern. This is needed for automated power
        cycling where the bootrom window is too short (<100ms) to detect
        0x20 and respond in time.
        """
        self._continuous_ack = enabled

    async def handshake(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> HandshakeResult:
        """Wait for bootrom 0x20 pattern and send 0xAA acknowledgment.

        When continuous_ack is enabled (via set_continuous_ack), sends 0xAA
        every ~50ms while waiting. This ensures the bootrom sees 0xAA
        immediately on startup, critical for fast-booting devices where the
        bootrom window is <100ms.
        """
        _emit(on_progress, ProgressEvent(
            stage=Stage.HANDSHAKE, bytes_sent=0, bytes_total=1,
            message="Waiting for bootrom... power-cycle the device now!",
        ))

        continuous = self._continuous_ack
        counter = 0
        while True:
            if continuous:
                await transport.write(BOOTMODE_ACK)

            try:
                byte = await transport.read(1, timeout=0.05 if continuous else 1.0)
            except TransportTimeout:
                continue

            if byte == b"\x00":
                continue
            if byte == BOOTMODE_MARKER:
                counter += 1
            else:
                counter = 0

            if counter >= BOOTMODE_COUNT:
                if not continuous:
                    await transport.flush_output()
                    await transport.write(BOOTMODE_ACK)
                await transport.flush_input()

                _emit(on_progress, ProgressEvent(
                    stage=Stage.HANDSHAKE, bytes_sent=1, bytes_total=1,
                    message="Boot mode entered",
                ))
                return HandshakeResult(success=True, message="Boot mode entered")

    async def _send_frame_with_retry(
        self,
        transport: Transport,
        frame_data: bytes,
        retries: int,
        timeout: float,
    ) -> bool:
        """Send a frame and wait for ACK, retrying on failure."""
        for _ in range(retries):
            await transport.flush_output()
            await transport.write(frame_data)
            await transport.flush_input()
            try:
                ack = await transport.read(1, timeout=timeout)
                if ack == ACK_BYTE:
                    return True
            except TransportTimeout:
                continue
            except Exception:
                continue
        return False

    async def _send_head(
        self, transport: Transport, length: int, address: int
    ) -> bool:
        frame = HeadFrame(length=length, address=address).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_SHORT, timeout=0.03
        )

    async def _send_data(
        self, transport: Transport, seq: int, payload: bytes
    ) -> bool:
        frame = DataFrame(seq=seq, payload=payload).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_LONG, timeout=0.15
        )

    async def _send_tail(self, transport: Transport, seq: int) -> bool:
        frame = TailFrame(seq=seq).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_SHORT, timeout=0.15
        )

    async def _send_ddr_step(
        self,
        transport: Transport,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> bool:
        """Send DDR initialization step (64 bytes to SRAM)."""
        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_INIT, bytes_sent=0, bytes_total=64,
            message="Sending DDR step",
        ))

        ddr_data = profile.ddr_step_data
        if not await self._send_head(transport, 64, profile.ddr_step_address):
            return False

        if not await self._send_data(transport, 1, ddr_data):
            return False

        if not await self._send_tail(transport, 2):
            return False

        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_INIT, bytes_sent=64, bytes_total=64,
            message="DDR step complete",
        ))
        return True

    async def _send_spl(
        self,
        transport: Transport,
        firmware: bytes,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        spl_override: bytes | None = None,
    ) -> bool:
        """Send SPL (secondary program loader) to SRAM."""
        spl_size = profile.spl_max_size
        if spl_override is not None:
            spl_data = spl_override[:spl_size].ljust(spl_size, b"\x00")
        else:
            spl_data = firmware[:spl_size]

        _emit(on_progress, ProgressEvent(
            stage=Stage.SPL, bytes_sent=0, bytes_total=spl_size,
            message="Sending SPL",
        ))

        if not await self._send_head(transport, spl_size, profile.spl_address):
            return False

        chunks = chunk_data(spl_data, MAX_DATA_LEN)
        for i, chunk in enumerate(chunks):
            seq = i + 1
            if not await self._send_data(transport, seq, chunk):
                return False
            _emit(on_progress, ProgressEvent(
                stage=Stage.SPL, bytes_sent=min(seq * MAX_DATA_LEN, spl_size),
                bytes_total=spl_size,
            ))

        if not await self._send_tail(transport, len(chunks) + 1):
            return False

        _emit(on_progress, ProgressEvent(
            stage=Stage.SPL, bytes_sent=spl_size, bytes_total=spl_size,
            message="SPL complete",
        ))
        return True

    async def _send_uboot(
        self,
        transport: Transport,
        firmware: bytes,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> bool:
        """Send U-Boot image to DDR."""
        total = len(firmware)

        _emit(on_progress, ProgressEvent(
            stage=Stage.UBOOT, bytes_sent=0, bytes_total=total,
            message="Sending U-Boot",
        ))

        if not await self._send_head(transport, total, profile.uboot_address):
            return False

        chunks = chunk_data(firmware, MAX_DATA_LEN)
        for i, chunk in enumerate(chunks):
            seq = i + 1
            if not await self._send_data(transport, seq, chunk):
                return False
            _emit(on_progress, ProgressEvent(
                stage=Stage.UBOOT, bytes_sent=min(seq * MAX_DATA_LEN, total),
                bytes_total=total,
            ))

        if not await self._send_tail(transport, len(chunks) + 1):
            return False

        _emit(on_progress, ProgressEvent(
            stage=Stage.UBOOT, bytes_sent=total, bytes_total=total,
            message="U-Boot complete",
        ))
        return True

    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        spl_override: bytes | None = None,
    ) -> RecoveryResult:
        if self._profile is None:
            return RecoveryResult(success=False, error="No profile loaded")

        profile = self._profile
        stages: list[Stage] = []

        if not await self._send_ddr_step(transport, profile, on_progress):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send DDR step",
            )
        stages.append(Stage.DDR_INIT)

        if not await self._send_spl(transport, firmware, profile, on_progress,
                                    spl_override=spl_override):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send SPL",
            )
        stages.append(Stage.SPL)

        if not await self._send_uboot(transport, firmware, profile, on_progress):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send U-Boot",
            )
        stages.append(Stage.UBOOT)

        _emit(on_progress, ProgressEvent(
            stage=Stage.COMPLETE, bytes_sent=1, bytes_total=1,
            message="Recovery complete",
        ))
        stages.append(Stage.COMPLETE)
        return RecoveryResult(success=True, stages_completed=stages)
