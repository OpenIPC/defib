"""Async Unix socket transport for connecting to QEMU chardev sockets.

Allows defib to connect to a QEMU instance using:
    -chardev socket,id=ser0,path=/tmp/sock,server=on,wait=off -serial chardev:ser0

Usage:
    defib burn -c hi3516ev300 -p socket:///tmp/sock
"""

from __future__ import annotations

import asyncio
import logging
import socket as sock_mod

from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)


class SocketTransport(Transport):
    """Transport over a Unix domain socket (SOCK_STREAM)."""

    def __init__(self, conn: sock_mod.socket) -> None:
        self._sock = conn
        self._sock.setblocking(False)
        self._buf = bytearray()

    @classmethod
    async def create(cls, path: str) -> SocketTransport:
        """Connect to a Unix domain socket at the given path."""
        try:
            s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
            s.setblocking(False)
            loop = asyncio.get_event_loop()
            await loop.sock_connect(s, path)
        except OSError as e:
            raise TransportError(f"Failed to connect to socket {path}: {e}") from e

        logger.info("Connected to QEMU socket: %s", path)
        return cls(s)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        # Return from internal buffer first
        if self._buf:
            chunk = bytes(self._buf[:size])
            del self._buf[:size]
            return chunk

        loop = asyncio.get_event_loop()
        try:
            if timeout is not None:
                data = await asyncio.wait_for(
                    loop.sock_recv(self._sock, size), timeout=timeout
                )
            else:
                data = await loop.sock_recv(self._sock, size)
        except TimeoutError:
            raise TransportTimeout(f"Read timeout ({timeout}s)")
        except OSError as e:
            raise TransportError(f"Socket read error: {e}") from e

        if not data:
            raise TransportError("Socket closed by remote end")

        return bytes(data)

    async def write(self, data: bytes) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.sock_sendall(self._sock, data)
        except OSError as e:
            raise TransportError(f"Socket write error: {e}") from e

    async def flush_input(self) -> None:
        # No-op for sockets: unlike serial ports, there is no stale data
        # to drain.  Flushing here would discard legitimate ACK responses
        # that QEMU has already sent back (sockets are fast, no serial
        # latency to separate the write from the ACK arrival).
        pass

    async def flush_output(self) -> None:
        pass  # sendall already ensures data is sent

    async def bytes_waiting(self) -> int:
        return len(self._buf)

    async def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
