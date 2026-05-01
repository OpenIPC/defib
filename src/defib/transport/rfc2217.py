"""RFC 2217 (Telnet COM Port Control) transport.

Wraps pyserial's ``serial.serial_for_url("rfc2217://host:port")``
backend so the rest of defib can talk to a remote UART bridge —
notably the OpenIPC Vectis daemon — exactly the way it talks to a
local serial port.

Compared with ``SocketTransport``:

- The data path is binary safe (``0x10`` and ``0xFF`` round-trip
  through the bridge unchanged); pyserial handles RFC 854 escaping
  and unescaping under the hood.
- Modem-control lines (``set_dtr``, ``set_rts``) and the UART baud
  rate (``set_baudrate``) are exposed as out-of-band RFC 2217
  sub-options instead of in-band magic bytes.

This is the transport ``VectisController`` uses to reset the
attached camera by pulsing RTS/DTR.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import serial

from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)


class Rfc2217Transport(Transport):
    """Transport over a pyserial RFC 2217 client.

    ``Serial`` instances returned by ``serial.serial_for_url`` are
    blocking; we route every IO call through the default thread pool
    via ``run_in_executor``.
    """

    def __init__(self, port: Any) -> None:
        # Typed as Any so mypy doesn't insist on serial.Serial — the
        # rfc2217 backend is serial.rfc2217.Serial, a sibling subclass
        # of SerialBase with the same public API.
        self._port = port
        # pyserial's RFC 2217 backend may return chunks larger than the
        # requested size (its read() loop appends a whole queue chunk
        # then exits when ``len(data) >= size``).  Stash overflow here
        # so callers always get exactly ``size`` bytes (or fewer on
        # timeout), matching the Transport ABC contract.
        self._buf = bytearray()

    # pyserial 3.5's rfc2217.Serial.timeout setter invokes
    # ``_reconfigure_port()`` which re-sends every port parameter
    # (baud/datasize/parity/stopsize/control/flow-control) as a
    # sub-negotiation, each costing a network round trip.  Mutating
    # ``timeout`` per-read makes a tight handshake loop ~400 ms slower
    # per iteration than the underlying pyserial read.  We therefore
    # set a *small fixed* pyserial timeout once at open and enforce
    # the caller's per-read timeout with our own loop in Python.
    _PYSERIAL_READ_QUANTUM = 0.01  # 10 ms

    @classmethod
    async def create(
        cls,
        url: str,
        baudrate: int = 115200,
    ) -> Rfc2217Transport:
        """Open an RFC 2217 connection and return a transport.

        ``url`` must be a fully-formed pyserial URL, e.g.
        ``"rfc2217://172.17.32.17:35241"``.  ``baudrate`` is sent as
        a ``SET-BAUDRATE`` sub-option during open.
        """
        import socket as _socket
        loop = asyncio.get_event_loop()

        def _open() -> Any:
            port = serial.serial_for_url(
                url,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=cls._PYSERIAL_READ_QUANTUM,
            )
            # Disable Nagle.  pyserial's RFC 2217 backend leaves the
            # default on, which on a high-RTT link (~40 ms) buffers
            # small writes for one RTT before sending.  That alone
            # closes the HiSilicon bootrom's ~100 ms 0x20-marker /
            # 0xAA-ack catch window — our 0xAA flood lands AFTER the
            # camera has already moved on to SPI boot.
            try:
                sock = port._socket
                if sock is not None:
                    sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except (AttributeError, OSError) as e:
                logger.debug("Could not enable TCP_NODELAY: %s", e)
            return port

        try:
            port = await loop.run_in_executor(None, _open)
        except (serial.SerialException, OSError) as e:
            raise TransportError(f"Failed to open {url}: {e}") from e

        logger.info("Connected to RFC 2217 server: %s @ %d baud", url, baudrate)
        return cls(port)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        loop = asyncio.get_event_loop()
        deadline = None if timeout is None else loop.time() + timeout
        buf = bytearray()

        # Service the local overflow buffer first.
        if self._buf:
            take = min(size, len(self._buf))
            buf.extend(self._buf[:take])
            del self._buf[:take]
            if len(buf) >= size:
                return bytes(buf)

        while True:
            remaining = size - len(buf)
            if remaining <= 0:
                return bytes(buf)
            chunk = await loop.run_in_executor(
                None, self._port.read, remaining
            )
            if chunk:
                if len(chunk) > remaining:
                    # pyserial returned a whole queue chunk that
                    # exceeds the requested size — keep the overflow
                    # for the next read so callers always get exactly
                    # what they asked for.
                    buf.extend(chunk[:remaining])
                    self._buf.extend(chunk[remaining:])
                else:
                    buf.extend(chunk)
                if len(buf) >= size:
                    return bytes(buf)
            if deadline is None:
                continue  # caller wants to wait forever
            if loop.time() >= deadline:
                if buf:
                    return bytes(buf)
                raise TransportTimeout(f"Read timeout ({timeout}s)")

    async def unread(self, data: bytes) -> None:
        """Push data back to the front of the read buffer."""
        new = bytearray(data)
        new.extend(self._buf)
        self._buf = new

    async def bytes_waiting(self) -> int:
        return len(self._buf) + int(self._port.in_waiting)

    async def write(self, data: bytes) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, self._port.write, data
        )

    async def flush_input(self) -> None:
        self._buf.clear()
        await asyncio.get_event_loop().run_in_executor(
            None, self._port.reset_input_buffer
        )

    async def flush_output(self) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, self._port.reset_output_buffer
        )

    async def close(self) -> None:
        if self._port is not None and self._port.is_open:
            await asyncio.get_event_loop().run_in_executor(
                None, self._port.close
            )

    # ------------------------------------------------------------------
    # RFC 2217 modem-control extensions (used by VectisController)
    # ------------------------------------------------------------------

    async def set_dtr(self, active: bool) -> None:
        """Set DTR.  Sends RFC 2217 SET-CONTROL 8 (on) or 9 (off)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: setattr(self._port, "dtr", active))

    async def set_rts(self, active: bool) -> None:
        """Set RTS.  Sends RFC 2217 SET-CONTROL 11 (on) or 12 (off)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: setattr(self._port, "rts", active))

    async def set_baudrate(self, baud: int) -> None:
        """Set the remote UART baud rate via SET-BAUDRATE."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: setattr(self._port, "baudrate", baud))
