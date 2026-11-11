"""Tests for SocketTransport — TCP-bridged UART semantics."""

from __future__ import annotations

import asyncio
import socket as sock_mod

import pytest

from defib.transport.base import TransportError, TransportTimeout
from defib.transport.socket import SocketTransport


async def _serve_one(listener: sock_mod.socket, send_on_connect: bytes = b"") -> sock_mod.socket:
    """Accept one client, optionally send some data, return the server-side fd."""
    loop = asyncio.get_event_loop()
    listener.setblocking(False)
    client, _ = await loop.sock_accept(listener)
    client.setblocking(False)
    if send_on_connect:
        await loop.sock_sendall(client, send_on_connect)
    return client


async def _pair_via_localhost() -> tuple[SocketTransport, sock_mod.socket, sock_mod.socket]:
    """Return (client SocketTransport, server-side socket, listener) — all on 127.0.0.1."""
    listener = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    host, port = listener.getsockname()

    accept_task = asyncio.create_task(_serve_one(listener))
    transport = await SocketTransport.create_tcp(host, port)
    server = await accept_task
    return transport, server, listener


class TestFlushInputDrainsStaleData:
    """The rack-pod failure mode: bridge buffers camera UART output
    while disconnected, dumps it to the host on accept. flush_input()
    must drain it so the boot protocol doesn't consume garbage as ACKs.
    """

    @pytest.mark.asyncio
    async def test_flush_drains_pending_socket_bytes(self) -> None:
        transport, server, listener = await _pair_via_localhost()
        try:
            loop = asyncio.get_event_loop()
            # Pre-load the kernel recv buffer with 16 bytes of "garbage"
            # — mirrors what a rack pod flushes on first read.
            await loop.sock_sendall(server, b"\xfe\x5e\x40\xff\x5e\x41\x5e\x40"
                                            b"\x5e\x40\x5e\x40\x40\x5e\x41\x30")
            # Let it land in the kernel buffer
            await asyncio.sleep(0.05)

            # flush_input must discard all of it
            await transport.flush_input()

            # Next read should not return the garbage — should time out.
            with pytest.raises(TransportTimeout):
                await transport.read(1, timeout=0.1)
        finally:
            await transport.close()
            server.close()
            listener.close()

    @pytest.mark.asyncio
    async def test_flush_lets_subsequent_real_data_through(self) -> None:
        """After flush, freshly written data still arrives."""
        transport, server, listener = await _pair_via_localhost()
        try:
            loop = asyncio.get_event_loop()
            await loop.sock_sendall(server, b"OLD")
            await asyncio.sleep(0.05)
            await transport.flush_input()

            await loop.sock_sendall(server, b"NEW")
            await asyncio.sleep(0.05)
            data = await transport.read(3, timeout=0.5)
            assert data == b"NEW"
        finally:
            await transport.close()
            server.close()
            listener.close()

    @pytest.mark.asyncio
    async def test_flush_on_empty_buffer_is_noop(self) -> None:
        """No pending data, no client closure — flush_input must not block or fail."""
        transport, server, listener = await _pair_via_localhost()
        try:
            await transport.flush_input()
            await transport.flush_input()  # idempotent
        finally:
            await transport.close()
            server.close()
            listener.close()


async def test_bytes_waiting_sees_data_not_yet_read() -> None:
    """Callers poll bytes_waiting() before read(); it must count bytes that
    are sitting in the kernel, not only ones read() already buffered."""
    import socket

    from defib.transport.socket import SocketTransport

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        assert await transport.bytes_waiting() == 0
        peer.sendall(b"\xbd\x00hello")
        for _ in range(100):
            if await transport.bytes_waiting():
                break
        assert await transport.bytes_waiting() == 7
        assert await transport.read(7, timeout=1.0) == b"\xbd\x00hello"
        assert await transport.bytes_waiting() == 0
    finally:
        await transport.close()
        peer.close()


async def test_read_loses_nothing_that_lands_on_its_timeout() -> None:
    """Bytes that come in as a read() times out are the next read()'s.

    wait_for(sock_recv()) threw them away: the receive had taken them from
    the socket when the timeout cancelled it. A peer that answers once after
    a long wait -- the Novatek ROM's WRLOADER echo -- was then never heard
    (OpenIPC/qemu-bench#265). Here a byte is sent around the end of each
    read's timeout, swept across it, and every byte must come out.
    """
    import socket
    import threading

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    timeout = 0.01
    trials = 120
    try:
        got = bytearray()
        for t in range(trials):
            delay = timeout + (t % 21 - 10) * 50e-6
            timer = threading.Timer(delay, peer.sendall, (bytes([t]),))
            timer.start()
            deadline = asyncio.get_running_loop().time() + 0.5
            while len(got) <= t and asyncio.get_running_loop().time() < deadline:
                try:
                    got += await transport.read(16, timeout=timeout)
                except TransportTimeout:
                    pass
            timer.join()
        assert bytes(got) == bytes(range(trials))
    finally:
        await transport.close()
        peer.close()


async def test_timed_out_read_hands_its_bytes_to_the_next() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        with pytest.raises(TransportTimeout):
            await transport.read(4, timeout=0.05)
        peer.sendall(b"echo-and-more")
        assert await transport.read(4, timeout=1.0) == b"echo"
        assert await transport.read(64, timeout=1.0) == b"-and-more"
    finally:
        await transport.close()
        peer.close()


async def test_flush_discards_a_timed_out_reads_bytes() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        with pytest.raises(TransportTimeout):
            await transport.read(4, timeout=0.05)
        peer.sendall(b"stale")
        await asyncio.sleep(0.05)
        await transport.flush_input()
        peer.sendall(b"fresh")
        assert await transport.read(16, timeout=1.0) == b"fresh"
    finally:
        await transport.close()
        peer.close()


async def test_bytes_waiting_keeps_order_behind_a_timed_out_read() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        with pytest.raises(TransportTimeout):
            await transport.read(16, timeout=0.05)
        peer.sendall(b"first")
        for _ in range(100):
            if await transport.bytes_waiting():
                break
            await asyncio.sleep(0.01)
        peer.sendall(b"second")
        await asyncio.sleep(0.05)
        out = bytearray()
        while len(out) < 11:
            out += await transport.read(16, timeout=1.0)
        assert bytes(out) == b"firstsecond"
    finally:
        await transport.close()
        peer.close()


async def test_overlapping_reads_never_get_the_same_bytes() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        a = asyncio.create_task(transport.read(3, timeout=1.0))
        b = asyncio.create_task(transport.read(3, timeout=1.0))
        await asyncio.sleep(0.02)
        peer.sendall(b"abcdef")
        got = await asyncio.gather(a, b)
        assert sorted(got) == [b"abc", b"def"]
    finally:
        await transport.close()
        peer.close()


async def test_flush_during_a_read_lets_it_wait_for_fresh_bytes() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        reader = asyncio.create_task(transport.read(16, timeout=1.0))
        await asyncio.sleep(0.02)
        await transport.flush_input()
        peer.sendall(b"fresh")
        assert await reader == b"fresh"
    finally:
        await transport.close()
        peer.close()


async def test_bytes_waiting_survives_a_flush_while_it_yields() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        with pytest.raises(TransportTimeout):
            await transport.read(4, timeout=0.02)
        poll = asyncio.create_task(transport.bytes_waiting())
        await transport.flush_input()  # runs while the poll is suspended
        assert await poll == 0
    finally:
        await transport.close()
        peer.close()


async def test_bytes_waiting_drops_a_receive_that_saw_eof() -> None:
    import socket

    ours, peer = socket.socketpair()
    transport = SocketTransport(ours)
    try:
        with pytest.raises(TransportTimeout):
            await transport.read(4, timeout=0.02)
        peer.close()
        for _ in range(10):
            await transport.bytes_waiting()
        assert transport._recv is None
        with pytest.raises(TransportError):
            await transport.read(4, timeout=0.5)
    finally:
        await transport.close()
