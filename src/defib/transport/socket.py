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
        # The receive a timed-out read() left running; the next one takes
        # its bytes (see read())
        self._recv: asyncio.Future[bytes] | None = None

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
        # One receive, waited on with asyncio.wait(), which never cancels it.
        # wait_for(sock_recv()) did: when bytes came in the loop iteration in
        # which its timeout ran out, sock_recv had already taken them from
        # the socket, the cancellation threw its result away, and the caller
        # saw a plain timeout. A reply landing on a timeout boundary was gone
        # for good -- the Novatek ROM's WRLOADER echo, one reply after
        # seconds of 0.5 s polls, failed qemu-bench's recovery test that way
        # (OpenIPC/qemu-bench#265). Now a receive that is still waiting at
        # the timeout stays running, and the next read() gets its bytes.
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            # Return from internal buffer first
            if self._buf:
                chunk = bytes(self._buf[:size])
                del self._buf[:size]
                return chunk
            if self._recv is None:
                self._recv = asyncio.ensure_future(
                    loop.sock_recv(self._sock, size)
                )
            recv = self._recv
            left = None if deadline is None else max(deadline - loop.time(), 0)
            done, _ = await asyncio.wait({recv}, timeout=left)
            if not done:
                raise TransportTimeout(f"Read timeout ({timeout}s)")
            if self._recv is not recv:
                # Another read() took this receive's bytes (into _buf for
                # what it did not need), or a flush dropped them: look again
                continue
            self._recv = None
            if recv.cancelled():
                raise TransportError("Socket read stopped: input flushed or closed")
            try:
                data = recv.result()
            except OSError as e:
                raise TransportError(f"Socket read error: {e}") from e

            if not data:
                raise TransportError("Socket closed by remote end")

            # It was started by an earlier read() that may have asked for more
            self._buf += data[size:]
            return bytes(data[:size])

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
        self._drop_recv()
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

    def _drop_recv(self) -> None:
        """Stop the receive a timed-out read() left running, discarding
        anything it took."""
        recv, self._recv = self._recv, None
        if recv is None:
            return
        if recv.done():
            if not recv.cancelled():
                recv.exception()  # retrieved: no "never retrieved" warning
        else:
            recv.cancel()

    async def bytes_waiting(self) -> int:
        # read() goes straight to the socket, so _buf alone only ever holds
        # bytes pulled in here or left over from a read.  Pull whatever the
        # kernel already has (the socket is non-blocking) so callers that
        # poll before reading see data that has actually arrived.
        if self._recv is not None:
            # A timed-out read()'s receive is still running: it gets the
            # bytes first, or they would come out of order. Let it run.
            await asyncio.sleep(0)
            recv = self._recv
            if recv is not None and recv.done():
                if recv.cancelled() or recv.exception() or not recv.result():
                    # Failed or at EOF: drop it, and poll the socket as
                    # before, which leaves the error to the next read()
                    self._drop_recv()
                else:
                    self._recv = None
                    self._buf += recv.result()
                    return len(self._buf)
            if self._recv is not None:
                return len(self._buf)
        try:
            data = self._sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            data = None
        except OSError:
            data = None  # let the next read() report the error
        if data:
            self._buf += data
        return len(self._buf)

    async def close(self) -> None:
        self._drop_recv()
        try:
            self._sock.close()
        except OSError:
            pass
