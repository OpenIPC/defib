"""Tests for SocketTransport — TCP-bridged UART semantics."""

from __future__ import annotations

import asyncio
import socket as sock_mod

import pytest

from defib.transport.base import TransportTimeout
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
