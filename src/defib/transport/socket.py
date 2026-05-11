"""Async socket transport for Unix-domain (QEMU) and TCP (Vectis) sockets.

Two URL schemes are supported via :func:`defib.transport.serial_platform.
create_transport`:

- ``socket:///tmp/sock`` — Unix-domain socket (QEMU chardev sockets).
- ``tcp://host:port``    — TCP/IP socket (e.g. OpenIPC Vectis UART bridge).

Both share the same non-blocking read/write implementation; only the
``connect()`` step differs.
"""

from __future__ import annotations

import asyncio
import logging
import socket as sock_mod

from defib.transport.base import Transport, TransportError, TransportTimeout

logger = logging.getLogger(__name__)


class SocketTransport(Transport):
    """Transport over a stream socket (AF_UNIX or AF_INET)."""

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

        logger.info("Connected to Unix socket: %s", path)
        return cls(s)

    @classmethod
    async def create_tcp(cls, host: str, port: int) -> SocketTransport:
        """Connect to a TCP socket at host:port."""
        try:
            s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
            s.setblocking(False)
            # TCP_NODELAY: small UART-style writes (e.g. single Ctrl+P
            # byte for Vectis) must not be delayed by Nagle's algorithm.
            s.setsockopt(sock_mod.IPPROTO_TCP, sock_mod.TCP_NODELAY, 1)
            loop = asyncio.get_event_loop()
            await loop.sock_connect(s, (host, port))
        except OSError as e:
            raise TransportError(
                f"Failed to connect to TCP {host}:{port}: {e}"
            ) from e

        logger.info("Connected to TCP socket: %s:%d", host, port)
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
        # Match SerialTransport.reset_input_buffer() — discard any pending
        # input. Necessary for TCP-bridged UARTs (rack pod, generic ser2net)
        # where the bridge buffers camera UART output while no client is
        # connected, then floods it on accept. Without draining, the boot
        # protocol's `_send_frame_with_retry` consumes the stale ASCII as
        # fake ACKs and burns its entire retry budget in <1 ms.
        self._buf.clear()
        while True:
            try:
                data = self._sock.recv(4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break

    async def flush_output(self) -> None:
        pass  # sendall already ensures data is sent

    async def bytes_waiting(self) -> int:
        return len(self._buf)

    async def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
