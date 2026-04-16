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
        self._continuous_ack = False

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

        Two modes controlled by set_continuous_ack():

        continuous_ack=True (automated power cycling):
            Floods 0xAA every ~50ms from the start. Safe because the device
            is guaranteed to be off/rebooting — no running OS to echo back.

        continuous_ack=False (manual power cycling, default):
            Waits silently until the device stops responding (power-off
            detected via timeout after receiving data), then floods 0xAA
            so the bootrom sees it immediately on next boot.
        """
        _emit(on_progress, ProgressEvent(
            stage=Stage.HANDSHAKE, bytes_sent=0, bytes_total=1,
            message="Waiting for bootrom... power-cycle the device now!",
        ))

        flooding = self._continuous_ack
        ever_saw_data = False
        counter = 0

        while True:
            if flooding:
                await transport.write(BOOTMODE_ACK)

            try:
                byte = await transport.read(1, timeout=0.05 if flooding else 1.0)
            except TransportTimeout:
                if not flooding and ever_saw_data:
                    # Was getting data, now silence — device powered off.
                    # Start flooding 0xAA so bootrom sees it on next boot.
                    logger.debug("Device went silent, flooding 0xAA")
                    flooding = True
                continue

            if byte == b"\x00":
                continue

            ever_saw_data = True

            if byte == BOOTMODE_MARKER:
                counter += 1
            else:
                counter = 0

            if counter >= BOOTMODE_COUNT:
                if not flooding:
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
            await transport.flush_input()
            await transport.flush_output()
            await transport.write(frame_data)
            try:
                ack = await transport.read(1, timeout=timeout)
                if ack == ACK_BYTE:
                    return True
            except TransportTimeout:
                continue
            except Exception:
                continue
        return False

    @staticmethod
    async def _rehandshake(transport: Transport) -> bool:
        """Re-enter boot mode after SPL runs.

        On some SoCs the SPL re-sends 0x20 bootmode markers after DDR init,
        requiring a fresh 0xAA acknowledgment before it will accept HEAD
        frames.  On SoCs that don't do this, the line stays quiet and we
        return True immediately.
        """
        import time
        marker_count = 0
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                data = await transport.read(1, timeout=0.2)
                if data == BOOTMODE_MARKER:
                    marker_count += 1
                    if marker_count >= BOOTMODE_COUNT:
                        await transport.write(BOOTMODE_ACK)
                        logger.debug("rehandshake: sent 0xAA after %d markers", marker_count)
                        return True
                elif data in (b"\x0a", b"\x0d"):
                    continue  # ignore newlines mixed into marker stream
                else:
                    # unexpected byte — not in marker mode
                    logger.debug("rehandshake: got 0x%02X, no re-handshake needed", data[0])
                    return True
            except TransportTimeout:
                if marker_count > 0:
                    # saw some markers but not enough — send ACK anyway
                    await transport.write(BOOTMODE_ACK)
                    logger.debug("rehandshake: sent 0xAA after partial %d markers", marker_count)
                    return True
                return True  # silence — device is ready without re-handshake
        return False  # deadline reached

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
        label: str = "U-Boot",
    ) -> bool:
        """Send U-Boot (or agent) image to DDR."""
        total = len(firmware)

        _emit(on_progress, ProgressEvent(
            stage=Stage.UBOOT, bytes_sent=0, bytes_total=total,
            message=f"Sending {label}",
        ))

        # After SPL runs DDR init, some SoCs (e.g. hi3516av200) re-send
        # 0x20 bootmode markers requiring a fresh 0xAA handshake.
        await self._rehandshake(transport)
        head = HeadFrame(length=total, address=profile.uboot_address).encode()
        if not await self._send_frame_with_retry(
            transport, head, retries=64, timeout=0.15,
        ):
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

        # Tail frame — best-effort for U-Boot stage.  Some SoCs (e.g.
        # hi3516av200) consider the transfer complete once they've received
        # all bytes declared in HEAD and don't ACK the TAIL.
        if not await self._send_tail(transport, len(chunks) + 1):
            logger.debug("U-Boot TAIL not ACKed (non-fatal, all data sent)")

        _emit(on_progress, ProgressEvent(
            stage=Stage.UBOOT, bytes_sent=total, bytes_total=total,
            message=f"{label} complete",
        ))
        return True

    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        spl_override: bytes | None = None,
        payload_label: str = "U-Boot",
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

        if not await self._send_uboot(transport, firmware, profile, on_progress,
                                       label=payload_label):
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
