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


def _frame_label(frame_data: bytes) -> str:
    """Return a human-readable label for a frame based on its magic byte(s)."""
    if len(frame_data) >= 4 and frame_data[0:4] == b"\xfe\x00\xff\x01":
        return "HEAD"
    if len(frame_data) >= 1:
        if frame_data[0] == 0xDA:
            return "DATA"
        if frame_data[0] == 0xED:
            return "TAIL"
    return "UNKNOWN"


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

    @property
    def uses_frame_blast_handshake(self) -> bool:
        """Whether this chip uses sendFrameForStart (HEAD blast) as handshake.

        When True, the caller should skip the separate handshake() call and
        let send_firmware() handle it internally via _send_frame_for_start().
        """
        return self._profile is not None and self._profile.prestep_data is not None

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
        total_markers = 0
        # Write a chunk per iteration in flooding mode — at 115200 baud the
        # UART can only clock ~11.5 KB/s, so a 64-byte burst keeps roughly
        # 5 ms of 0xAA on the wire continuously and saturates the bootrom's
        # ~100 ms catch window even with TCP/RFC 2217 round-trip latency.
        BURST = b"\xaa" * 64

        while True:
            if flooding:
                await transport.write(BURST)

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
                total_markers += 1
                if total_markers % 10 == 0:
                    logger.debug("handshake: %d bootmode markers (0x20) so far", total_markers)
            else:
                logger.debug("handshake: unexpected byte 0x%02X (counter reset)", byte[0])
                counter = 0

            if counter >= BOOTMODE_COUNT:
                logger.debug(
                    "handshake: %d consecutive markers, entering boot mode (flooding=%s)",
                    counter, flooding,
                )
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
        label = _frame_label(frame_data)
        hex_preview = frame_data[:30].hex(" ")
        logger.debug(
            "TX %s (%d bytes): %s%s",
            label, len(frame_data), hex_preview,
            "..." if len(frame_data) > 30 else "",
        )
        for attempt in range(retries):
            await transport.flush_input()
            await transport.flush_output()
            try:
                await transport.write(frame_data)
                ack = await transport.read(1, timeout=timeout)
                if ack == ACK_BYTE:
                    logger.debug("TX %s ACKed (attempt %d/%d)", label, attempt + 1, retries)
                    return True
                logger.debug(
                    "TX %s got 0x%02X instead of ACK (attempt %d/%d)",
                    label, ack[0], attempt + 1, retries,
                )
            except TransportTimeout:
                if attempt < 3 or attempt == retries - 1:
                    logger.debug("TX %s timeout (attempt %d/%d)", label, attempt + 1, retries)
                continue
            except Exception as e:
                logger.debug("TX %s error: %s (attempt %d/%d)", label, e, attempt + 1, retries)
                continue
        logger.debug("TX %s FAILED after %d retries", label, retries)
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
        logger.debug("rehandshake: waiting up to 5s for bootmode markers")
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
        logger.debug("HEAD: length=%d address=0x%08X", length, address)
        frame = HeadFrame(length=length, address=address).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_SHORT, timeout=0.15
        )

    async def _send_data(
        self, transport: Transport, seq: int, payload: bytes
    ) -> bool:
        logger.debug("DATA: seq=%d payload=%d bytes", seq, len(payload))
        frame = DataFrame(seq=seq, payload=payload).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_LONG, timeout=0.15
        )

    async def _send_tail(self, transport: Transport, seq: int) -> bool:
        logger.debug("TAIL: seq=%d", seq)
        frame = TailFrame(seq=seq).encode()
        return await self._send_frame_with_retry(
            transport, frame, FRAME_SEND_RETRIES_SHORT, timeout=0.15
        )

    async def _send_frame_for_start(
        self, transport: Transport, profile: SoCProfile
    ) -> bool:
        """HiTool-style handshake: blast 0xAA + HEAD(FILELEN0, ADDRESS0) until ACK.

        Continuously sends 0xAA (to trigger bootrom download mode) followed
        by the 14-byte HEAD frame for up to 30 seconds.  The bootrom enters
        download mode on 0xAA, then ACKs the HEAD frame.
        """
        import time

        addr = profile.ddr_step_address
        head_frame = HeadFrame(length=64, address=addr).encode()
        logger.debug(
            "sendFrameForStart: blasting 0xAA+HEAD(64, 0x%08X) for 30s",
            addr,
        )

        deadline = time.monotonic() + 30.0
        attempt = 0
        while time.monotonic() < deadline:
            await transport.write(BOOTMODE_ACK + head_frame)
            attempt += 1
            try:
                ack = await transport.read(1, timeout=0.05)
                if ack == ACK_BYTE:
                    logger.debug(
                        "sendFrameForStart: ACK after %d blasts", attempt
                    )
                    await transport.flush_input()
                    return True
            except TransportTimeout:
                continue
        logger.debug("sendFrameForStart: timeout after 30s (%d blasts)", attempt)
        return False

    async def _send_ddr_step(
        self,
        transport: Transport,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> str | None:
        """Send DDR initialization steps to SRAM.

        Matches HiTool's exact sequence:
        1. sendFrameForStart: blast HEAD(64, ADDRESS0) until ACK (handshake)
        2. For each step (PRESTEP0, DDRSTEP0, PRESTEP1): HEAD+DATA+TAIL

        Returns ``None`` on success, or a string describing which sub-step
        failed.  Distinguishes handshake failure from frame-send failures
        so error messages aren't misleading.
        """
        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_INIT, bytes_sent=0, bytes_total=64,
            message="Sending DDR step",
        ))

        addr = profile.ddr_step_address
        prestep = profile.prestep_data

        # sendFrameForStart: blast HEAD as handshake (HiTool approach)
        if prestep is not None:
            if not await self._send_frame_for_start(transport, profile):
                return "handshake (sendFrameForStart) timed out"

            # PRESTEP0: HEAD+DATA+TAIL (HEAD sent again per HiTool's loop)
            logger.debug(
                "=== PRESTEP0 === address=0x%08X data=%d bytes",
                addr, len(prestep),
            )
            if not await self._send_head(transport, 64, addr):
                return "PRESTEP0 HEAD frame not ACKed"
            if not await self._send_data(transport, 1, prestep):
                return "PRESTEP0 DATA frame not ACKed"
            if not await self._send_tail(transport, 2):
                logger.debug("PRESTEP0 TAIL not ACKed (non-fatal)")

        # DDRSTEP0: actual DDR init trigger
        logger.debug(
            "=== DDR STEP === address=0x%08X data=%d bytes",
            addr, len(profile.ddr_step_data),
        )
        ddr_data = profile.ddr_step_data
        if not await self._send_head(transport, 64, addr):
            return "DDRSTEP0 HEAD frame not ACKed"

        if not await self._send_data(transport, 1, ddr_data):
            return "DDRSTEP0 DATA frame not ACKed"

        if not await self._send_tail(transport, 2):
            logger.debug("DDRSTEP0 TAIL not ACKed (non-fatal)")

        # PRESTEP1: DDR training verification (waits for DDR to be ready)
        prestep1 = profile.prestep1_data
        if prestep1 is not None:
            logger.debug(
                "=== PRESTEP1 === address=0x%08X data=%d bytes",
                addr, len(prestep1),
            )
            if not await self._send_head(transport, len(prestep1), addr):
                return "PRESTEP1 HEAD frame not ACKed"
            if not await self._send_data(transport, 1, prestep1):
                return "PRESTEP1 DATA frame not ACKed"
            if not await self._send_tail(transport, 2):
                logger.debug("PRESTEP1 TAIL not ACKed (non-fatal)")

        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_INIT, bytes_sent=64, bytes_total=64,
            message="DDR step complete",
        ))
        return None

    @staticmethod
    def _detect_spl_size(
        firmware: bytes,
        profile_max: int,
        sram_limit: int | None = None,
    ) -> int:
        """Detect actual SPL code size from firmware binary.

        HiSilicon mini-boot layout: vector table + .reg + executable code +
        compressed U-Boot payload (LZMA or gzip, depending on the build).
        The SPL only needs the code region — bytes past the compressed
        payload boundary land in SRAM that the bootrom uses for its own
        stack/state, so writing them corrupts the bootrom.

        When a compressed-payload boundary is found we trust it absolutely
        and use it instead of profile_max — even if it's smaller. profile_max
        comes from HiTool's reference SPL which fills the full window; an
        OpenIPC build that's more compact must NOT be padded to that size.

        sram_limit is the chip's actual SRAM-window ceiling (spl_address to
        SRAM end). Set on profiles where the firmware can have a compressed
        payload boundary that lies past the chip's real SRAM ceiling — e.g.
        single-blob mini-boot self-extractors (hi3520dv200 OpenIPC build:
        LZMA at 0x4400 vs ~0x3B00 SRAM window). Writing past sram_limit
        corrupts bootrom state and the chip re-enters boot-mode mid-upload.
        Cap the detected boundary so the upload fits.
        """
        # LZMA: 0x5D + 4-byte LE dictionary size (64K..16M)
        VALID_LZMA_DICT = {1 << n for n in range(16, 25)}
        # gzip: 1f 8b 08 (deflate method)
        for i in range(0x4000, min(len(firmware), 0x10000)):
            b = firmware[i]
            detected: int | None = None
            if b == 0x5D:
                ds = int.from_bytes(firmware[i + 1 : i + 5], "little")
                if ds in VALID_LZMA_DICT:
                    detected = i & ~0x3FF
                    label = "LZMA"
            elif b == 0x1F and firmware[i + 1] == 0x8B and firmware[i + 2] == 0x08:
                detected = i & ~0x3FF
                label = "gzip"
            if detected is not None:
                if sram_limit is not None and detected > sram_limit:
                    logger.info(
                        "SPL boundary detected (%s) at 0x%X exceeds SRAM "
                        "limit 0x%X; capping at SRAM limit",
                        label, detected, sram_limit,
                    )
                    detected = sram_limit
                if detected != profile_max:
                    logger.info(
                        "SPL boundary detected (%s) at 0x%X (%d bytes); "
                        "profile default was 0x%X (%d bytes)",
                        label, detected, detected, profile_max, profile_max,
                    )
                return detected
        return profile_max

    async def _send_spl(
        self,
        transport: Transport,
        firmware: bytes,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        spl_override: bytes | None = None,
    ) -> bool:
        """Send SPL (secondary program loader) to SRAM."""
        # Detect SPL boundary from the buffer we're actually going to send.
        # When spl_override is set (agent flash flow), the agent binary itself
        # has no gzip/LZMA payload to act as a boundary marker; scanning it
        # falls back to profile_max, which on av300 is 0x6000 — past the
        # 12-byte 0xFF padding at 0x52E4 that hangs the cv500-family bootrom.
        # Detecting from spl_override gives 0x5000 (gzip at 0x52F0 rounded
        # down) and excludes the FF run.
        scan_buf = spl_override if spl_override is not None else firmware
        spl_size = self._detect_spl_size(
            scan_buf, profile.spl_max_size, sram_limit=profile.spl_sram_limit,
        )
        logger.debug(
            "=== SPL === address=0x%08X size=%d chunks=%d",
            profile.spl_address, spl_size,
            (spl_size + MAX_DATA_LEN - 1) // MAX_DATA_LEN,
        )
        if spl_override is not None:
            spl_data = spl_override[:spl_size].ljust(spl_size, b"\x00")
        else:
            spl_data = firmware[:spl_size]
        # Defense-in-depth: zero any ≥12-byte 0xFF runs even after the
        # boundary fix, so non-OpenIPC SPL builds with FF padding earlier
        # in the binary don't trip the same cv500-family bootrom RX bug.
        spl_data = self._zero_long_ff_runs(spl_data)

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
            # av200/av300 SPL detaches the bootrom protocol handler as
            # soon as all declared bytes arrive — no TAIL ACK is sent.
            if profile.prestep_data is None:
                return False
            logger.debug("SPL TAIL not ACKed (non-fatal for av200/av300, all data sent)")

        # DDR training delay: HiTool sleeps 300ms after SPL transfer.
        # Always apply — the SPL runs DDR training which needs time.
        import asyncio as _asyncio
        logger.debug("DDR training delay: 300ms (spl_size=%d)", spl_size)
        await _asyncio.sleep(0.3)

        _emit(on_progress, ProgressEvent(
            stage=Stage.SPL, bytes_sent=spl_size, bytes_total=spl_size,
            message="SPL complete",
        ))
        return True

    @staticmethod
    def _zero_long_ff_runs(firmware: bytes, threshold: int = 12) -> bytes:
        """Zero out long runs of 0xFF bytes.

        The hi3516cv500-family bootrom (av300, dv300, cv500) hangs mid-DATA
        frame when the payload contains >=12 consecutive 0xFF bytes — almost
        certainly a quirk in the bootrom's UART receive path.  These runs
        only appear as inert padding between SPL code and the compressed
        U-Boot payload, so zeroing them is safe.
        """
        if firmware.count(b"\xff" * threshold) == 0:
            return firmware
        out = bytearray(firmware)
        run_start = -1
        for i, b in enumerate(out):
            if b == 0xFF:
                if run_start < 0:
                    run_start = i
            else:
                if run_start >= 0 and i - run_start >= threshold:
                    logger.debug(
                        "_zero_long_ff_runs: zeroed %d 0xFF bytes at offset 0x%X",
                        i - run_start, run_start,
                    )
                    for j in range(run_start, i):
                        out[j] = 0
                run_start = -1
        if run_start >= 0 and len(out) - run_start >= threshold:
            for j in range(run_start, len(out)):
                out[j] = 0
        return bytes(out)

    async def _send_uboot(
        self,
        transport: Transport,
        firmware: bytes,
        profile: SoCProfile,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        label: str = "U-Boot",
    ) -> bool:
        """Send U-Boot (or agent) image to DDR."""
        firmware = self._zero_long_ff_runs(firmware)
        total = len(firmware)
        logger.debug(
            "=== %s === address=0x%08X total=%d chunks=%d",
            label, profile.uboot_address, total,
            (total + MAX_DATA_LEN - 1) // MAX_DATA_LEN,
        )

        _emit(on_progress, ProgressEvent(
            stage=Stage.UBOOT, bytes_sent=0, bytes_total=total,
            message=f"Sending {label}",
        ))

        # After SPL runs DDR init, some SoCs re-send 0x20 bootmode markers.
        # For chips with PRESTEP0 (frame-blast), use the full blast approach
        # since simple rehandshake isn't sufficient on av200.
        if profile.prestep_data is not None:
            if not await self._send_frame_for_start(transport, profile):
                return False
        else:
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
        logger.debug(
            "send_firmware: profile=%s firmware=%d bytes spl_override=%s",
            profile.name, len(firmware), spl_override is not None,
        )

        ddr_err = await self._send_ddr_step(transport, profile, on_progress)
        if ddr_err is not None:
            return RecoveryResult(
                success=False, stages_completed=stages,
                error=f"DDR init failed: {ddr_err}",
            )
        stages.append(Stage.DDR_INIT)

        logger.debug("send_firmware: rehandshake before SPL")
        await self._rehandshake(transport)

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
