"""Recovery session orchestrator.

Ties together protocol, transport, and profile to perform a complete
device recovery. Emits events for UI consumption.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from defib.profiles.loader import load_profile
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.protocol.registry import find_protocol
from defib.recovery.events import (
    LogEvent,
    ProgressEvent,
    RecoveryResult,
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
    ) -> None:
        self.chip = chip.lower()
        self._firmware_path = firmware_path
        self._firmware_data = firmware_data
        self._protocol_cls = find_protocol(self.chip)

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
    ) -> RecoveryResult:
        """Execute the full recovery: handshake → firmware transfer.

        Args:
            transport: Communication transport (serial, mock, etc.)
            on_progress: Callback for progress events.
            on_log: Callback for log events.
            send_break: If True, send Ctrl-C after upload to enter U-Boot console.

        Returns:
            RecoveryResult with success status.
        """
        start_time = time.monotonic()

        protocol = self._protocol_cls()

        # For standard protocol, load and attach the SoC profile
        if isinstance(protocol, HiSiliconStandard):
            profile = load_profile(self.chip)
            protocol.set_profile(profile)
            if on_log:
                on_log(LogEvent(level="info", message=f"Loaded profile: {profile.name}"))

        # Handshake
        if on_log:
            on_log(LogEvent(
                level="info",
                message=f"Starting {self._protocol_cls.name()} handshake for {self.chip}",
            ))

        handshake = await protocol.handshake(transport, on_progress)
        if not handshake.success:
            elapsed = (time.monotonic() - start_time) * 1000
            if on_log:
                on_log(LogEvent(level="error", message=f"Handshake failed: {handshake.message}"))
            return RecoveryResult(
                success=False,
                error=f"Handshake failed: {handshake.message}",
                elapsed_ms=elapsed,
            )

        if on_log:
            on_log(LogEvent(level="info", message=handshake.message))

        # Firmware transfer
        firmware = self._load_firmware()
        if on_log:
            on_log(LogEvent(
                level="info",
                message=f"Sending {len(firmware)} bytes of firmware...",
            ))

        result = await protocol.send_firmware(transport, firmware, on_progress)
        result.elapsed_ms = (time.monotonic() - start_time) * 1000

        # Send break (Ctrl-C) if requested
        if send_break and result.success:
            if on_log:
                on_log(LogEvent(level="info", message="Sending Ctrl-C to enter U-Boot console"))
            for _ in range(49):
                await transport.write(b"\x03")
                try:
                    await transport.read(1, timeout=0.05)
                except Exception:
                    pass

        if on_log:
            if result.success:
                on_log(LogEvent(
                    level="info",
                    message=f"Recovery complete in {result.elapsed_ms:.0f}ms",
                ))
            else:
                on_log(LogEvent(level="error", message=f"Recovery failed: {result.error}"))

        return result
