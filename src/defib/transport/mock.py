"""Mock transport for testing — feeds pre-defined RX data and captures TX data."""

from __future__ import annotations

import asyncio
from collections import deque

from defib.transport.base import Transport, TransportTimeout


class MockTransport(Transport):
    """A transport that replays predefined responses for testing.

    Enqueue expected device responses with `enqueue_rx()`, then run
    protocol code against this transport. After the test, inspect
    `tx_log` to verify what was sent.

    Args:
        flush_clears_buffer: If False, flush_input() is a no-op (useful for
            tests where the protocol calls flush_input between stages).
    """

    def __init__(self, flush_clears_buffer: bool = True) -> None:
        self._rx_queue: deque[bytes] = deque()
        self._rx_buffer: bytearray = bytearray()
        self.tx_log: list[bytes] = []
        self._closed = False
        self._flush_clears = flush_clears_buffer

    def enqueue_rx(self, data: bytes) -> None:
        """Add data that will be returned by future read() calls."""
        self._rx_buffer.extend(data)

    def enqueue_rx_chunks(self, *chunks: bytes) -> None:
        """Add multiple chunks of RX data."""
        for chunk in chunks:
            self._rx_buffer.extend(chunk)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if self._closed:
            return b""

        if not self._rx_buffer:
            if timeout is not None and timeout <= 0:
                raise TransportTimeout("No data available")
            # Give a brief chance for data to appear (in real async usage)
            if timeout is not None:
                await asyncio.sleep(min(timeout, 0.001))
            if not self._rx_buffer:
                raise TransportTimeout(f"No data available (wanted {size} bytes)")

        count = min(size, len(self._rx_buffer))
        result = bytes(self._rx_buffer[:count])
        del self._rx_buffer[:count]
        return result

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        self.tx_log.append(bytes(data))

    async def flush_input(self) -> None:
        if self._flush_clears:
            self._rx_buffer.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self._rx_buffer)

    async def unread(self, data: bytes) -> None:
        """Push data back to the front of the read buffer."""
        new_buf = bytearray(data) + self._rx_buffer
        self._rx_buffer = new_buf

    async def close(self) -> None:
        self._closed = True

    @property
    def all_tx_data(self) -> bytes:
        """All transmitted data concatenated."""
        return b"".join(self.tx_log)
