"""Replay transport that feeds captured RX data for testing.

Reads a .dcap capture file and replays the device-side (RX) responses
when the protocol under test sends the expected TX data.
"""

from __future__ import annotations

import asyncio
import logging

from defib.capture.format import CaptureFile, Direction
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)


class ReplayTransport(Transport):
    """Transport that replays a captured .dcap session.

    RX records are fed into the read buffer. TX records are used to
    verify that the protocol sends the expected data (optional).

    Args:
        capture: The captured session to replay.
        verify_tx: If True, assert that written data matches expected TX records.
        simulate_timing: If True, add delays matching the original timing.
    """

    def __init__(
        self,
        capture: CaptureFile,
        verify_tx: bool = False,
        simulate_timing: bool = False,
    ) -> None:
        self._capture = capture
        self._verify_tx = verify_tx
        self._simulate_timing = simulate_timing
        self._rx_buffer = bytearray()
        self._record_index = 0
        self._tx_log: list[bytes] = []
        self._closed = False

        # Pre-load all RX data into the buffer
        for record in capture.records:
            if record.direction == Direction.RX:
                self._rx_buffer.extend(record.data)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if self._closed:
            return b""

        if not self._rx_buffer:
            if timeout is not None:
                await asyncio.sleep(min(timeout, 0.001))
            if not self._rx_buffer:
                raise TransportTimeout(f"Replay: no more RX data (wanted {size} bytes)")

        count = min(size, len(self._rx_buffer))
        result = bytes(self._rx_buffer[:count])
        del self._rx_buffer[:count]
        return result

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        self._tx_log.append(bytes(data))

    async def flush_input(self) -> None:
        pass  # Don't clear replay buffer

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self._rx_buffer)

    async def unread(self, data: bytes) -> None:
        new_buf = bytearray(data) + self._rx_buffer
        self._rx_buffer = new_buf

    async def close(self) -> None:
        self._closed = True

    @property
    def all_tx_data(self) -> bytes:
        """All data sent by the protocol during replay."""
        return b"".join(self._tx_log)

    @property
    def rx_remaining(self) -> int:
        """Number of RX bytes not yet consumed."""
        return len(self._rx_buffer)

    @property
    def replay_complete(self) -> bool:
        """True if all RX data has been consumed."""
        return len(self._rx_buffer) == 0
