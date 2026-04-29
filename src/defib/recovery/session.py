"""Recovery session orchestrator.

Ties together protocol, transport, and profile to perform a complete
device recovery. Emits events for UI consumption.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from defib.power.base import PowerController
from defib.profiles.loader import load_profile
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.protocol.registry import find_protocol
from defib.recovery.events import (
    HandshakeResult,
    LogEvent,
    ProgressEvent,
    RecoveryResult,
    Stage,
)
from defib.transport.base import Transport

logger = logging.getLogger(__name__)


class RecoverySession:
    """Orchestrates a complete device recovery session.

    Usage:
        session = RecoverySession(chip="hi3516cv300", firmware_path="u-boot.bin")
        result = await session.run(transport, on_progress=callback)
    """

    def __init__(
        self,
        chip: str,
        firmware_path: str | None = None,
        firmware_data: bytes | None = None,
        power_controller: PowerController | None = None,
        poe_port: str | None = None,
    ) -> None:
        self.chip = chip.lower()
        self._firmware_path = firmware_path
        self._firmware_data = firmware_data
        self._protocol_cls = find_protocol(self.chip)
        self._power = power_controller
        self._poe_port = poe_port

    @property
    def protocol_name(self) -> str:
        return self._protocol_cls.name()

    def _load_firmware(self) -> bytes:
        if self._firmware_data is not None:
            return self._firmware_data
        if self._firmware_path is not None:
            with open(self._firmware_path, "rb") as f:
                return f.read()
        raise ValueError("No firmware data or path provided")

    async def run(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        on_log: Callable[[LogEvent], None] | None = None,
        send_break: bool = False,
        max_handshake_attempts: int = 2,
    ) -> RecoveryResult:
        """Execute the full recovery: handshake → firmware transfer.

        Args:
            transport: Communication transport (serial, mock, etc.)
            on_progress: Callback for progress events.
            on_log: Callback for log events.
            send_break: If True, send Ctrl-C after upload to enter U-Boot console.
            max_handshake_attempts: Retry the power-cycle + handshake + DDR-init
                phase up to this many times if the transient handshake fails.
                Only applies when programmatic power control is configured.

        Returns:
            RecoveryResult with success status.
        """
        start_time = time.monotonic()

        protocol = self._protocol_cls()

        # For standard protocol, load and attach the SoC profile
        if isinstance(protocol, HiSiliconStandard):
            profile = load_profile(self.chip)
            protocol.set_profile(profile)
            if self._power:
                protocol.set_continuous_ack(True)
            if on_log:
                on_log(LogEvent(level="info", message=f"Loaded profile: {profile.name}"))

        # Determine if this chip uses frame-blast handshake (built into send_firmware)
        frame_blast = (
            isinstance(protocol, HiSiliconStandard) and protocol.uses_frame_blast_handshake
        )

        # Retry the transient phase (power-cycle + handshake + DDR init) up
        # to ``max_handshake_attempts`` times.  Only meaningful when we have
        # programmatic power control — manual power cycling would require
        # human re-intervention.
        can_retry = bool(self._power and self._poe_port)
        attempts = max_handshake_attempts if can_retry else 1

        firmware = self._load_firmware()
        handshake: HandshakeResult | None = None
        last_attempt_error: str | None = None

        for attempt in range(1, attempts + 1):
            if attempt > 1 and on_log:
                on_log(LogEvent(
                    level="warn",
                    message=(
                        f"Retrying handshake (attempt {attempt}/{attempts}) — "
                        f"previous error: {last_attempt_error}"
                    ),
                ))

            # Power cycle
            if self._power and self._poe_port:
                if on_log:
                    on_log(LogEvent(
                        level="info",
                        message=f"Power-cycling device on {self._poe_port}...",
                    ))
                if on_progress:
                    on_progress(ProgressEvent(
                        stage=Stage.POWER_CYCLE, bytes_sent=0, bytes_total=1,
                        message=f"Power-cycling {self._poe_port}...",
                    ))

                try:
                    await self._power.power_cycle(self._poe_port)
                except Exception as e:
                    elapsed = (time.monotonic() - start_time) * 1000
                    if on_log:
                        on_log(LogEvent(level="error", message=f"Power cycle failed: {e}"))
                    return RecoveryResult(
                        success=False,
                        error=f"Power cycle failed: {e}",
                        elapsed_ms=elapsed,
                    )

                # Drain serial until line stays quiet for 500ms.  Replaces a
                # fixed 2-second sleep + flush_input — that approach can miss
                # late-arriving stale bytes (the camera may still be powering
                # down when the flush runs) and isn't robust against pyserial
                # buffer caveats.  Quiet-detection is deterministic: a
                # powered-off chip cannot transmit.
                discarded = await transport.drain_until_silent(
                    quiet_period=0.5, max_wait=5.0,
                )
                if discarded and on_log:
                    on_log(LogEvent(
                        level="info",
                        message=f"Drained {discarded} stale bytes from serial",
                    ))

                if on_progress:
                    on_progress(ProgressEvent(
                        stage=Stage.POWER_CYCLE, bytes_sent=1, bytes_total=1,
                        message="Power cycle complete",
                    ))

            # Handshake — skip for frame-blast chips (handled inside send_firmware)
            if frame_blast:
                if on_log:
                    on_log(LogEvent(
                        level="info",
                        message=f"Using sendFrameForStart handshake for {self.chip}",
                    ))
                handshake = HandshakeResult(success=True, message="Frame-blast (deferred)")
            elif self._power and self._poe_port:
                # Power-cycle mode with 0x20→0xAA handshake: flood 0xAA
                if on_log:
                    on_log(LogEvent(
                        level="info",
                        message=f"Starting {self._protocol_cls.name()} handshake for {self.chip}",
                    ))
                import asyncio as _asyncio
                handshake_task = _asyncio.create_task(
                    protocol.handshake(transport, on_progress)
                )
                handshake = await handshake_task
            else:
                # Manual power cycling — just start handshake and wait
                if on_log:
                    on_log(LogEvent(
                        level="info",
                        message=f"Starting {self._protocol_cls.name()} handshake for {self.chip}",
                    ))
                handshake = await protocol.handshake(transport, on_progress)

            # If non-frame-blast handshake failed and we can retry, try again
            if not handshake.success:
                last_attempt_error = f"handshake: {handshake.message}"
                if attempt < attempts:
                    continue
                break

            # Handshake OK (or deferred for frame-blast).  Send firmware.
            if on_log:
                on_log(LogEvent(
                    level="info",
                    message=f"Sending {len(firmware)} bytes of firmware...",
                ))
            send_result = await protocol.send_firmware(
                transport, firmware, on_progress,
            )
            if send_result.success:
                # Mutate handshake variable so post-loop code sees success.
                handshake_succeeded_result = send_result
                break

            # Firmware send failed — only retry if it failed in the early
            # handshake/DDR phase (frame-blast handshake or DDR init).
            # Once we're past DDR init, retrying is unlikely to help and
            # costs another 30+ seconds of upload time.
            err = send_result.error or ""
            is_early = (
                Stage.DDR_INIT not in send_result.stages_completed
            )
            if is_early and attempt < attempts:
                last_attempt_error = err
                continue

            # Either past-DDR failure or final attempt — bail out.
            handshake_succeeded_result = send_result
            break
        else:
            # Loop completed without break — all retries exhausted on handshake
            elapsed = (time.monotonic() - start_time) * 1000
            if on_log:
                on_log(LogEvent(
                    level="error",
                    message=f"Handshake failed after {attempts} attempts: {last_attempt_error}",
                ))
            return RecoveryResult(
                success=False,
                error=f"Handshake failed: {last_attempt_error}",
                elapsed_ms=elapsed,
            )

        result = handshake_succeeded_result
        result.elapsed_ms = (time.monotonic() - start_time) * 1000

        # Send break (Ctrl-C) to interrupt U-Boot autoboot
        if send_break and result.success:
            if on_log:
                on_log(LogEvent(
                    level="info",
                    message="Waiting for U-Boot to start (up to 15s)...",
                ))
            # U-Boot needs time to decompress, relocate, and initialize
            # hardware (SPI, NAND, MMC, network) before showing the
            # autoboot countdown. This can take 5-10 seconds.
            # Strategy: send Ctrl-C every 200ms while reading output,
            # looking for the autoboot prompt or U-Boot console prompt.
            import asyncio
            start_break = time.monotonic()
            prompt_found = False
            buf = bytearray()
            while time.monotonic() - start_break < 15.0:
                await transport.write(b"\x03")
                try:
                    data = await transport.read(256, timeout=0.2)
                    buf.extend(data)
                    text = buf.decode("ascii", errors="replace")
                    # Check for autoboot in full accumulated text
                    if "autoboot" in text.lower():
                        if on_log:
                            on_log(LogEvent(level="info", message="Autoboot detected, sending Ctrl-C..."))
                        for _ in range(20):
                            await transport.write(b"\x03")
                            await asyncio.sleep(0.1)
                        prompt_found = True
                        break
                    # Check for U-Boot prompt only in the LAST chunk
                    # (avoid false matches on boot log substrings)
                    tail = text[-256:] if len(text) > 256 else text
                    if "OpenIPC #" in tail or "hisilicon #" in tail or "\n=> " in tail:
                        prompt_found = True
                        break
                except Exception:
                    pass

            if prompt_found:
                if on_log:
                    on_log(LogEvent(level="info", message="U-Boot console ready"))
            else:
                if on_log:
                    on_log(LogEvent(
                        level="warn",
                        message="U-Boot prompt not detected within 15s",
                    ))

        # Note: completion/failure is already reported via on_progress
        # (Stage.COMPLETE event). We only log errors here that weren't
        # already surfaced by the protocol.
        if on_log and not result.success:
            on_log(LogEvent(level="error", message=f"Recovery failed: {result.error}"))

        return result
