"""Abstract base class for boot recovery protocols."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from defib.recovery.events import HandshakeResult, ProgressEvent, RecoveryResult
from defib.transport.base import Transport


class ProtocolError(Exception):
    """Protocol-level error during recovery."""


class BootProtocol(ABC):
    """Base class for all boot recovery protocols.

    Each protocol implementation handles a specific family of SoCs
    with its own handshake and data transfer mechanisms.
    """

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Human-readable protocol name."""

    @classmethod
    @abstractmethod
    def matches(cls, chip_name: str) -> bool:
        """Return True if this protocol handles the given SoC chip name."""

    @abstractmethod
    async def handshake(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> HandshakeResult:
        """Perform initial bootrom handshake over the transport.

        Args:
            transport: The communication transport (serial, mock, etc.)
            on_progress: Optional callback for progress events.

        Returns:
            HandshakeResult with success status and any detected chip info.
        """

    @abstractmethod
    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> RecoveryResult:
        """Send firmware data through the protocol's staging sequence.

        Args:
            transport: The communication transport.
            firmware: Raw firmware binary data.
            on_progress: Optional callback for progress events.

        Returns:
            RecoveryResult indicating success or failure.
        """
