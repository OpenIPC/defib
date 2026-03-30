"""Abstract transport interface for serial and mock communication."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class TransportError(Exception):
    """Base exception for transport errors."""


class TransportTimeout(TransportError):
    """Read operation timed out."""


class Transport(ABC):
    """Abstract base class for bidirectional byte-stream transports.

    All protocol code communicates through this interface, enabling
    real serial ports, mock replay transports, and TCP-based transports
    to be used interchangeably.
    """

    @abstractmethod
    async def read(self, size: int, timeout: float | None = None) -> bytes:
        """Read up to `size` bytes. Raises TransportTimeout if timeout expires.

        Args:
            size: Maximum number of bytes to read.
            timeout: Seconds to wait. None means wait forever.

        Returns:
            Bytes read (may be shorter than size).

        Raises:
            TransportTimeout: If timeout expires before any data is available.
        """

    @abstractmethod
    async def write(self, data: bytes) -> None:
        """Write data to the transport."""

    @abstractmethod
    async def flush_input(self) -> None:
        """Discard any buffered input data."""

    @abstractmethod
    async def flush_output(self) -> None:
        """Ensure all buffered output has been sent."""

    @abstractmethod
    async def bytes_waiting(self) -> int:
        """Return the number of bytes available for reading without blocking."""

    async def drain_input(self) -> bytes:
        """Read and discard all currently buffered input. Returns discarded bytes."""
        waiting = await self.bytes_waiting()
        if waiting > 0:
            return await self.read(waiting, timeout=0.1)
        return b""

    async def read_exact(self, size: int, timeout: float | None = None) -> bytes:
        """Read exactly `size` bytes, raising TransportTimeout on timeout."""
        buf = bytearray()
        remaining = size
        deadline = None
        if timeout is not None:
            deadline = asyncio.get_event_loop().time() + timeout

        while remaining > 0:
            if deadline is not None:
                time_left = deadline - asyncio.get_event_loop().time()
                if time_left <= 0:
                    raise TransportTimeout(
                        f"Timed out reading {size} bytes (got {len(buf)})"
                    )
            else:
                time_left = None

            chunk = await self.read(remaining, timeout=time_left)
            if not chunk:
                raise TransportTimeout(
                    f"Timed out reading {size} bytes (got {len(buf)})"
                )
            buf.extend(chunk)
            remaining -= len(chunk)

        return bytes(buf)

    async def unread(self, data: bytes) -> None:
        """Push data back to the front of the read buffer.

        Not all transports support this. Default raises NotImplementedError.
        Used when a protocol reads more bytes than needed and wants to
        return the excess for the next operation.
        """
        raise NotImplementedError("This transport does not support unread()")

    async def close(self) -> None:
        """Close the transport. Default implementation does nothing."""
