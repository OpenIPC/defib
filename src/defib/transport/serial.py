"""Async serial transport wrapping pyserial-asyncio."""

from __future__ import annotations

import asyncio
import logging
import sys

import serial
import serial.tools.list_ports

from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 115200


class SerialTransport(Transport):
    """Transport implementation using pyserial for real serial ports."""

    def __init__(self, port: serial.Serial) -> None:
        self._port = port

    @classmethod
    async def create(
        cls,
        device: str,
        baudrate: int = DEFAULT_BAUDRATE,
    ) -> SerialTransport:
        """Open a serial port and return a transport.

        On macOS, applies workarounds for known USB-serial driver issues.
        """
        try:
            port = serial.Serial(
                port=device,
                baudrate=baudrate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=None,
                dsrdtr=False,       # Don't toggle DTR (may be wired to reset)
                rtscts=False,       # Don't use hardware flow control
            )
            port.dtr = False        # Keep DTR deasserted to avoid reset
        except serial.SerialException as e:
            raise TransportError(f"Failed to open {device}: {e}") from e

        transport = cls(port)

        # macOS workaround: drain any stale bytes after opening
        if sys.platform == "darwin":
            logger.info("macOS detected: draining stale bytes from serial port")
            port.timeout = 0.1
            try:
                stale = port.read(4096)
                if stale:
                    logger.debug("Drained %d stale bytes", len(stale))
            except Exception:
                pass
            port.timeout = None

        return transport

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        old_timeout = self._port.timeout
        try:
            self._port.timeout = timeout
            coro = asyncio.get_event_loop().run_in_executor(
                None, self._port.read, size
            )
            # Guard against pyserial blocking despite the timeout
            guard = timeout * 2 + 1.0 if timeout is not None else None
            data = await asyncio.wait_for(coro, timeout=guard)
            if not data and timeout is not None:
                raise TransportTimeout(f"Read timeout ({timeout}s)")
            return bytes(data)
        except asyncio.TimeoutError:
            raise TransportTimeout(f"Read timeout ({timeout}s, asyncio guard)")
        finally:
            self._port.timeout = old_timeout

    async def write(self, data: bytes) -> None:
        # Pyserial honours write_timeout inside Serial.write(), so the worker
        # thread actually returns instead of blocking in pselect6 forever.
        # asyncio.wait_for can't help here — cancelling a run_in_executor
        # future leaves the underlying thread still blocked.
        # 5s ceiling: a 1KB write at 115200 baud is ~89ms; >5s means the
        # kernel TX buffer isn't draining (USB-serial hung, flow control,
        # cable unplugged).
        old_wt = self._port.write_timeout
        self._port.write_timeout = 5.0
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, self._port.write, data
            )
        except serial.SerialTimeoutException as exc:
            raise TransportTimeout(
                f"Write timeout (5.0s, {len(data)} bytes)"
            ) from exc
        finally:
            self._port.write_timeout = old_wt

    async def flush_input(self) -> None:
        self._port.reset_input_buffer()

    async def flush_output(self) -> None:
        self._port.reset_output_buffer()

    async def set_baudrate(self, baud: int) -> None:
        self._port.baudrate = baud

    async def bytes_waiting(self) -> int:
        return int(self._port.in_waiting)

    async def close(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
