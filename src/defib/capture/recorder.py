"""Recording transport wrapper that captures all I/O to a .dcap file."""

from __future__ import annotations

import time

from defib.capture.format import CaptureFile
from defib.transport.base import Transport


class RecordingTransport(Transport):
    """Wraps a real transport, recording all reads and writes.

    Usage:
        real_transport = await SerialTransport.create("/dev/ttyUSB0")
        recording = RecordingTransport(real_transport, chip="hi3516cv300")
        # ... use recording as normal transport ...
        recording.capture.save("session.dcap")
    """

    def __init__(
        self,
        inner: Transport,
        chip: str = "",
        baudrate: int = 115200,
    ) -> None:
        self._inner = inner
        self.capture = CaptureFile(baudrate=baudrate, chip=chip)
        self._start_time = time.monotonic()

    def _timestamp_us(self) -> int:
        """Microseconds since recording started."""
        return int((time.monotonic() - self._start_time) * 1_000_000)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        data = await self._inner.read(size, timeout)
        if data:
            self.capture.add_rx(self._timestamp_us(), data)
        return data

    async def write(self, data: bytes) -> None:
        self.capture.add_tx(self._timestamp_us(), data)
        await self._inner.write(data)

    async def flush_input(self) -> None:
        await self._inner.flush_input()

    async def flush_output(self) -> None:
        await self._inner.flush_output()

    async def bytes_waiting(self) -> int:
        return await self._inner.bytes_waiting()

    async def unread(self, data: bytes) -> None:
        await self._inner.unread(data)

    async def close(self) -> None:
        await self._inner.close()
