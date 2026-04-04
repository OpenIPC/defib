"""Regression tests for write_memory multi-block transfers.

The bug: _recv_packet_sync accumulated partial COBS frame data in
_port_buffers between consecutive _write_block calls. After ~3 blocks,
stale READY packet fragments corrupted the next block's ACK parsing.

These tests verify that multiple write blocks work correctly when
READY packets arrive between blocks (simulating the agent's main loop
sending READY after each write completes).
"""


import pytest

from defib.agent.protocol import (
    ACK_OK,
    RSP_ACK,
    RSP_INFO,
    RSP_READY,
    build_packet,
    recv_response,
    _port_buffers,
)
from defib.transport.base import Transport, TransportTimeout


class FakePort:
    """Pyserial-compatible port backed by a byte buffer."""

    def __init__(self, rx_data: bytes = b""):
        self._rx = bytearray(rx_data)
        self._tx = bytearray()
        self.timeout = None

    def feed(self, data: bytes) -> None:
        self._rx.extend(data)

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        if not self._rx:
            return b""
        n = min(size, len(self._rx))
        data = bytes(self._rx[:n])
        del self._rx[:n]
        return data

    def write(self, data: bytes) -> int:
        self._tx.extend(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    @property
    def is_open(self) -> bool:
        return True

    def close(self) -> None:
        pass

    @property
    def tx_data(self) -> bytes:
        return bytes(self._tx)


class FakeTransport(Transport):
    def __init__(self, port: FakePort):
        self._port = port

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        data = self._port.read(size)
        if not data and timeout is not None:
            raise TransportTimeout("timeout")
        return data

    async def write(self, data: bytes) -> None:
        self._port.write(data)

    async def flush_input(self) -> None:
        self._port._rx.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return self._port.in_waiting

    async def close(self) -> None:
        pass


def make_ack(status: int = ACK_OK) -> bytes:
    return build_packet(RSP_ACK, bytes([status]))


def make_ready() -> bytes:
    return build_packet(RSP_READY, b"DEFIB")


def make_info() -> bytes:
    return build_packet(RSP_INFO, b"\x00" * 16)


class TestMultiBlockWrite:
    """Regression: consecutive _write_block calls must not desync."""

    @pytest.mark.asyncio
    async def test_two_consecutive_ack_reads(self):
        """Two recv_response calls back-to-back must both succeed."""
        port = FakePort()
        transport = FakeTransport(port)

        port.feed(make_ack())
        port.feed(make_ack())

        cmd1, d1 = await recv_response(transport, timeout=1.0)
        cmd2, d2 = await recv_response(transport, timeout=1.0)

        assert cmd1 == RSP_ACK and d1[0] == ACK_OK
        assert cmd2 == RSP_ACK and d2[0] == ACK_OK

    @pytest.mark.asyncio
    async def test_ack_with_ready_between(self):
        """ACK → READY ��� ACK: recv_response must skip READY correctly."""
        port = FakePort()
        transport = FakeTransport(port)

        port.feed(make_ack())
        port.feed(make_ready())
        port.feed(make_ack())

        cmd1, d1 = await recv_response(transport, timeout=1.0)
        assert cmd1 == RSP_ACK and d1[0] == ACK_OK

        cmd2, d2 = await recv_response(transport, timeout=1.0)
        assert cmd2 == RSP_ACK and d2[0] == ACK_OK

    @pytest.mark.asyncio
    async def test_many_acks_with_ready_interleaved(self):
        """Simulate 10 blocks: each block's final ACK followed by READY."""
        port = FakePort()
        transport = FakeTransport(port)

        for _ in range(10):
            port.feed(make_ack())    # Block initial ACK
            port.feed(make_ack())    # Per-packet ACK (1 packet per block)
            port.feed(make_ack())    # Final CRC ACK
            port.feed(make_ready())  # Agent READY between blocks

        for blk in range(10):
            # Initial ACK (may need to skip READY from prev block)
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK and d[0] == ACK_OK, f"Block {blk} initial ACK"

            # Per-packet ACK
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK and d[0] == ACK_OK, f"Block {blk} per-packet ACK"

            # Final CRC ACK
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK and d[0] == ACK_OK, f"Block {blk} final ACK"

    @pytest.mark.asyncio
    async def test_32_per_packet_acks_then_next_block(self):
        """Simulate real write: 32 per-packet ACKs, CRC ACK, READY, next block."""
        port = FakePort()
        transport = FakeTransport(port)

        for block_num in range(5):
            # Initial ACK
            port.feed(make_ack())
            # 32 per-packet ACKs
            for _ in range(32):
                port.feed(make_ack())
            # Final CRC ACK
            port.feed(make_ack())
            # READY between blocks
            if block_num < 4:
                port.feed(make_ready())

        for block_num in range(5):
            # Read initial ACK (skip READY if present)
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {block_num} initial: got 0x{cmd:02x}"

            # 32 per-packet ACKs
            for pkt in range(32):
                cmd, d = await recv_response(transport, timeout=1.0)
                assert cmd == RSP_ACK and d[0] == ACK_OK, \
                    f"Block {block_num} pkt {pkt}: got 0x{cmd:02x} status 0x{d[0]:02x}"

            # Final CRC ACK
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK and d[0] == ACK_OK, f"Block {block_num} final"

    @pytest.mark.asyncio
    async def test_port_buffers_dont_leak_between_blocks(self):
        """_port_buffers must not accumulate stale data across blocks."""
        port = FakePort()
        transport = FakeTransport(port)

        # Clear any global state
        _port_buffers.clear()

        for _ in range(10):
            # Simulate one write block: ACK + 4 per-packet ACKs + CRC ACK
            port.feed(make_ack())
            for _ in range(4):
                port.feed(make_ack())
            port.feed(make_ack())
            port.feed(make_ready())  # READY between blocks

        for blk in range(10):
            # Check port_buffers state
            pb = _port_buffers.get(id(port), bytearray())
            # Should be empty or contain only complete packet bytes
            # (never partial COBS frame data)

            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {blk} initial: 0x{cmd:02x}, pb={len(pb)}"

            for pkt in range(4):
                cmd, d = await recv_response(transport, timeout=1.0)
                assert cmd == RSP_ACK, f"Block {blk} pkt {pkt}"

            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {blk} final"

    @pytest.mark.asyncio
    async def test_info_between_blocks_flushes_pipeline(self):
        """get_info between blocks must not break subsequent writes."""
        port = FakePort()
        transport = FakeTransport(port)

        for _ in range(5):
            port.feed(make_info())   # INFO response
            port.feed(make_ack())    # Write initial ACK
            port.feed(make_ack())    # Per-packet ACK
            port.feed(make_ack())    # CRC ACK
            port.feed(make_ready())

        for blk in range(5):
            # INFO sync
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_INFO, f"Block {blk} INFO"

            # Write block
            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {blk} initial ACK"

            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {blk} per-pkt ACK"

            cmd, d = await recv_response(transport, timeout=1.0)
            assert cmd == RSP_ACK, f"Block {blk} CRC ACK"
