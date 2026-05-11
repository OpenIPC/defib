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

    async def drain_until_silent(
        self,
        quiet_period: float = 0.5,
        max_wait: float = 5.0,
    ) -> int:
        """Read and discard incoming data until the line stays quiet.

        Reads bytes with short timeouts, restarting the quiet timer whenever
        any data arrives.  Returns once no data has been seen for
        ``quiet_period`` seconds, or once ``max_wait`` total seconds elapsed.

        This is more reliable than a fixed sleep + ``flush_input`` because
        ``flush_input`` only drains the buffer at one moment in time — bytes
        arriving immediately after survive.  Reading until silence guarantees
        a clean state when the chip is physically off (which can't transmit).

        Returns the number of bytes discarded.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max_wait
        last_data_at = loop.time()
        discarded = 0

        await self.flush_input()
        while loop.time() < deadline:
            if loop.time() - last_data_at >= quiet_period:
                return discarded
            try:
                chunk = await self.read(256, timeout=0.05)
                if chunk:
                    discarded += len(chunk)
                    last_data_at = loop.time()
            except TransportTimeout:
                continue
        return discarded

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

    async def set_baudrate(self, baud: int) -> None:
        """Change the UART baud rate on both ends of the link.

        Real serial transports set their pyserial ``baudrate`` property.
        RFC 2217 sends a SET-BAUDRATE sub-option to the remote bridge.
        Bridges that expose an out-of-band control channel (e.g. the
        rack pod's ``POST /uart/baud``) call into it.

        Plain TCP-bridged UARTs that have no signalling for baud rate
        changes raise ``NotImplementedError`` and the caller must keep
        the wire at ``115200``.
        """
        raise NotImplementedError("This transport does not support set_baudrate()")

    async def close(self) -> None:
        """Close the transport. Default implementation does nothing."""
