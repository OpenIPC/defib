"""Tests for agent protocol: recv_packet, send_packet, wait_for_ready.

Uses a fake serial port (BytesIO-based) to simulate device responses
without real hardware.
"""

import struct

import pytest

from defib.agent.protocol import (
    ACK_OK,
    ACK_CRC_ERROR,
    CMD_INFO,
    CMD_READ,
    CMD_SELFUPDATE,
    CMD_WRITE,
    RSP_ACK,
    RSP_CRC32,
    RSP_DATA,
    RSP_INFO,
    RSP_READY,
    build_packet,
    parse_packet,
    _recv_packet_sync,
    recv_packet,
    recv_response,
    send_packet,
    wait_for_ready,
)
from defib.transport.base import Transport, TransportTimeout


# ---------------------------------------------------------------------------
# Fake serial port that behaves like pyserial's Serial
# ---------------------------------------------------------------------------

class FakePort:
    """Minimal pyserial-compatible port backed by a byte buffer."""

    def __init__(self, rx_data: bytes = b""):
        self._rx = bytearray(rx_data)
        self._tx = bytearray()
        self.timeout = None

    def feed(self, data: bytes) -> None:
        """Add bytes to the RX buffer (simulates device sending data)."""
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

    @property
    def is_open(self) -> bool:
        return True

    def close(self) -> None:
        pass

    @property
    def tx_data(self) -> bytes:
        return bytes(self._tx)


# ---------------------------------------------------------------------------
# Fake transport that wraps FakePort
# ---------------------------------------------------------------------------

class FakeTransport(Transport):
    """Transport wrapping a FakePort so recv_packet uses the sync path."""

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
        self._port._tx.clear()

    async def bytes_waiting(self) -> int:
        return self._port.in_waiting

    async def close(self) -> None:
        pass


def make_device_packet(cmd: int, data: bytes = b"") -> bytes:
    """Build a COBS-framed packet as the device would send it."""
    return build_packet(cmd, data)


# ---------------------------------------------------------------------------
# Tests: _recv_packet_sync
# ---------------------------------------------------------------------------

class TestRecvPacketSync:
    def test_single_packet(self):
        pkt = make_device_packet(RSP_INFO, b"\x01\x02\x03\x04" * 4)
        port = FakePort(pkt)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_INFO
        assert data == b"\x01\x02\x03\x04" * 4

    def test_ready_packet(self):
        pkt = make_device_packet(RSP_READY, b"DEFIB")
        port = FakePort(pkt)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_READY
        assert data == b"DEFIB"

    def test_multiple_packets_returns_first(self):
        pkt1 = make_device_packet(RSP_READY, b"DEFIB")
        pkt2 = make_device_packet(RSP_INFO, b"\x00" * 16)
        port = FakePort(pkt1 + pkt2)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_READY

    def test_garbage_before_valid_packet(self):
        garbage = b"\x01\x02\x03\x00"  # Invalid COBS frame (CRC fail)
        valid = make_device_packet(RSP_INFO, b"\xAA" * 16)
        port = FakePort(garbage + valid)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_INFO

    def test_empty_frames_skipped(self):
        # Multiple 0x00 delimiters followed by a valid packet
        delimiters = b"\x00\x00\x00"
        valid = make_device_packet(RSP_ACK, bytes([ACK_OK]))
        port = FakePort(delimiters + valid)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_ACK
        assert data == bytes([ACK_OK])

    def test_timeout_raises(self):
        port = FakePort(b"")
        with pytest.raises(TransportTimeout):
            _recv_packet_sync(port, timeout=0.1)

    def test_oversized_frame_discarded(self):
        # Frame larger than MAX_PACKET_SIZE should be discarded
        huge = bytes(range(1, 256)) * 5  # >1100 bytes, no 0x00
        valid = make_device_packet(RSP_READY, b"DEFIB")
        port = FakePort(huge + b"\x00" + valid)
        cmd, data = _recv_packet_sync(port, timeout=1.0)
        assert cmd == RSP_READY


# ---------------------------------------------------------------------------
# Tests: recv_packet (async, routes to sync for transport with _port)
# ---------------------------------------------------------------------------

class TestRecvPacket:
    @pytest.mark.asyncio
    async def test_via_fake_transport(self):
        pkt = make_device_packet(RSP_INFO, b"\x01" * 16)
        port = FakePort(pkt)
        transport = FakeTransport(port)
        cmd, data = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_INFO
        assert len(data) == 16

    @pytest.mark.asyncio
    async def test_timeout(self):
        port = FakePort(b"")
        transport = FakeTransport(port)
        with pytest.raises(TransportTimeout):
            await recv_packet(transport, timeout=0.1)

    @pytest.mark.asyncio
    async def test_async_path_preserves_post_delimiter_bytes(self):
        """Regression: recv_packet over a pyserial-less transport used
        to drop any bytes that arrived in the same chunk *after* a
        packet's delimiter. That dropped the RSP_ACK in
        RSP_DATA+RSP_ACK back-to-back responses, hanging read_memory().

        Drive a transport that has no `_port` attribute so the async
        path is exercised; deliver two whole packets in one chunk and
        assert both come out.
        """
        from defib.transport.mock import MockTransport

        p1 = make_device_packet(RSP_DATA, b"\x00\x00" + b"hello\x00world")
        p2 = make_device_packet(RSP_ACK, bytes([ACK_OK]))

        t = MockTransport(flush_clears_buffer=False)
        # Both packets land in one chunk — that's the case the async
        # parser previously mishandled.
        t.enqueue_rx(p1 + p2)

        cmd1, data1 = await recv_packet(t, timeout=1.0)
        cmd2, data2 = await recv_packet(t, timeout=1.0)

        assert cmd1 == RSP_DATA
        assert cmd2 == RSP_ACK
        assert data2 == bytes([ACK_OK])

    @pytest.mark.asyncio
    async def test_async_path_three_packets_one_chunk(self):
        from defib.transport.mock import MockTransport

        pkts = (
            make_device_packet(RSP_READY, b"") +
            make_device_packet(RSP_DATA, b"\x00\x00" + b"chunk1") +
            make_device_packet(RSP_ACK, bytes([ACK_OK]))
        )
        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(pkts)

        seen = []
        for _ in range(3):
            cmd, _ = await recv_packet(t, timeout=1.0)
            seen.append(cmd)
        assert seen == [RSP_READY, RSP_DATA, RSP_ACK]


# ---------------------------------------------------------------------------
# Tests: recv_packet async-leftover buffer stress
#
# These exercise the per-transport buffer that recv_packet keeps for
# bytes that arrived after a frame's delimiter — a regression class
# (PR #86) that's worth pinning down with stream-style scenarios:
# split frames across reads, large multi-packet streams, READY
# interleave, per-transport isolation, and timeout behaviour when
# the buffer has incomplete frame data.
# ---------------------------------------------------------------------------

class TestRecvPacketAsyncLeftoverStress:
    @pytest.mark.asyncio
    async def test_frame_split_across_two_reads_recombines(self) -> None:
        """A single frame can arrive split across two transport reads
        (typical for large TCP packets that cross MTU). The parser
        must accumulate until the delimiter and parse cleanly."""
        from defib.transport.mock import MockTransport
        pkt = make_device_packet(RSP_INFO, b"X" * 32)
        # Split the packet at an arbitrary mid-frame byte.
        split = len(pkt) // 2
        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(pkt[:split])
        t.enqueue_rx(pkt[split:])

        cmd, data = await recv_packet(t, timeout=1.0)
        assert cmd == RSP_INFO
        assert data == b"X" * 32

    @pytest.mark.asyncio
    async def test_large_stream_50_packets_in_one_chunk(self) -> None:
        """Stress: 50 small packets crammed into one chunk should
        all come out, in order. Catches off-by-one bugs in the
        leftover slicing."""
        from defib.transport.mock import MockTransport
        N = 50
        payloads = [bytes([i, 0xAA, 0x55]) for i in range(N)]
        stream = b"".join(
            make_device_packet(RSP_DATA, b"\x00\x00" + p) for p in payloads
        )
        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(stream)

        for i in range(N):
            cmd, data = await recv_packet(t, timeout=1.0)
            assert cmd == RSP_DATA
            assert data == b"\x00\x00" + payloads[i], f"packet {i} mismatch"

    @pytest.mark.asyncio
    async def test_recv_response_skips_ready_in_leftover(self) -> None:
        """The READY-skipping logic of recv_response (used by INFO,
        CRC32, etc.) must work even when the READY and the real
        response are coalesced into a single chunk via leftover."""
        from defib.transport.mock import MockTransport
        chunk = (
            make_device_packet(RSP_READY, b"DEFIB")
            + make_device_packet(RSP_READY, b"DEFIB")
            + make_device_packet(RSP_INFO,  b"PAYLOAD!")
            + make_device_packet(RSP_READY, b"DEFIB")  # trailing READY queued
        )
        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(chunk)

        cmd, data = await recv_response(t, timeout=1.0)
        assert cmd == RSP_INFO
        assert data == b"PAYLOAD!"
        # The trailing READY is still parseable on the next call —
        # leftover survived recv_response's internal recv_packet calls.
        cmd2, _ = await recv_packet(t, timeout=1.0)
        assert cmd2 == RSP_READY

    @pytest.mark.asyncio
    async def test_per_transport_isolation(self) -> None:
        """Two transports must not share leftover state. If they did,
        bytes from one socket would surface on another's read."""
        from defib.transport.mock import MockTransport
        pkt_a = make_device_packet(RSP_INFO, b"AAA")
        pkt_b = make_device_packet(RSP_DATA, b"\x00\x00BBB")
        ta = MockTransport(flush_clears_buffer=False)
        tb = MockTransport(flush_clears_buffer=False)
        # Two whole packets per transport — leftover gets populated.
        ta.enqueue_rx(pkt_a + pkt_a)
        tb.enqueue_rx(pkt_b + pkt_b)

        # Interleave reads
        ca, _ = await recv_packet(ta, timeout=1.0)
        cb, _ = await recv_packet(tb, timeout=1.0)
        ca2, _ = await recv_packet(ta, timeout=1.0)
        cb2, _ = await recv_packet(tb, timeout=1.0)

        assert ca  == RSP_INFO
        assert ca2 == RSP_INFO
        assert cb  == RSP_DATA
        assert cb2 == RSP_DATA

    @pytest.mark.asyncio
    async def test_incomplete_frame_in_leftover_blocks_until_timeout(self) -> None:
        """A leftover containing only PART of a frame (no delimiter yet)
        must wait for more data and time out cleanly if none arrives —
        never spuriously return a partial frame."""
        from defib.transport.mock import MockTransport
        pkt = make_device_packet(RSP_DATA, b"\x00\x00" + b"Z" * 16)
        # Half the packet only, no delimiter.
        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(pkt[: len(pkt) // 2])

        with pytest.raises(TransportTimeout):
            await recv_packet(t, timeout=0.2)

    @pytest.mark.asyncio
    async def test_corrupt_frame_skipped_then_recovers(self) -> None:
        """A frame that fails CRC mid-stream must be discarded and the
        parser must recover to the next valid frame."""
        from defib.transport.mock import MockTransport
        # Build a packet, then flip a bit in the middle to corrupt
        # the CRC. The parser should clear that frame and pick up the
        # next valid one.
        good = make_device_packet(RSP_INFO, b"GOOD")
        broken = bytearray(make_device_packet(RSP_DATA, b"\x00\x00" + b"BAD!"))
        broken[4] ^= 0x42   # flip a payload bit → CRC32 mismatch
        ok2 = make_device_packet(RSP_ACK, bytes([ACK_OK]))

        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(bytes(broken) + good + ok2)

        # First call: parser sees broken frame (CRC fail, discards),
        # then sees the GOOD packet.
        cmd, data = await recv_packet(t, timeout=1.0)
        assert cmd == RSP_INFO
        assert data == b"GOOD"
        # Second call should hit the trailing ACK via leftover.
        cmd2, _ = await recv_packet(t, timeout=1.0)
        assert cmd2 == RSP_ACK


# ---------------------------------------------------------------------------
# Tests: send_packet
# ---------------------------------------------------------------------------

class TestSendPacket:
    @pytest.mark.asyncio
    async def test_sends_cobs_framed_packet(self):
        port = FakePort()
        transport = FakeTransport(port)
        await send_packet(transport, CMD_INFO)
        tx = port.tx_data
        assert tx[-1:] == b"\x00"  # COBS delimiter
        # Should be parseable
        cmd, data = parse_packet(tx[:-1])
        assert cmd == CMD_INFO

    @pytest.mark.asyncio
    async def test_sends_with_data(self):
        port = FakePort()
        transport = FakeTransport(port)
        payload = struct.pack("<II", 0x41000000, 1024)
        await send_packet(transport, CMD_READ, payload)
        tx = port.tx_data
        cmd, data = parse_packet(tx[:-1])
        assert cmd == CMD_READ
        assert struct.unpack("<II", data) == (0x41000000, 1024)


# ---------------------------------------------------------------------------
# Tests: recv_response (skips READY)
# ---------------------------------------------------------------------------

class TestRecvResponse:
    @pytest.mark.asyncio
    async def test_skips_ready_packets(self):
        ready = make_device_packet(RSP_READY, b"DEFIB")
        info = make_device_packet(RSP_INFO, b"\x00" * 16)
        port = FakePort(ready + ready + info)
        transport = FakeTransport(port)
        cmd, data = await recv_response(transport, timeout=2.0)
        assert cmd == RSP_INFO

    @pytest.mark.asyncio
    async def test_returns_non_ready_immediately(self):
        ack = make_device_packet(RSP_ACK, bytes([ACK_OK]))
        port = FakePort(ack)
        transport = FakeTransport(port)
        cmd, data = await recv_response(transport, timeout=1.0)
        assert cmd == RSP_ACK
        assert data == bytes([ACK_OK])


# ---------------------------------------------------------------------------
# Tests: wait_for_ready
# ---------------------------------------------------------------------------

class TestWaitForReady:
    @pytest.mark.asyncio
    async def test_finds_ready(self):
        pkt = make_device_packet(RSP_READY, b"DEFIB")
        port = FakePort(pkt)
        transport = FakeTransport(port)
        result = await wait_for_ready(transport, timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_skips_non_ready(self):
        info = make_device_packet(RSP_INFO, b"\x00" * 16)
        ready = make_device_packet(RSP_READY, b"DEFIB")
        port = FakePort(info + ready)
        transport = FakeTransport(port)
        result = await wait_for_ready(transport, timeout=2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        port = FakePort(b"")
        transport = FakeTransport(port)
        result = await wait_for_ready(transport, timeout=0.2)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: data stream (simulated READ response)
# ---------------------------------------------------------------------------

class TestDataStream:
    @pytest.mark.asyncio
    async def test_read_multiple_data_packets_then_ack(self):
        """Simulate agent READ response: N DATA packets + final ACK."""
        port = FakePort()
        transport = FakeTransport(port)

        # Build stream: 3 DATA packets + ACK
        for seq in range(3):
            chunk = bytes(range(seq * 10, seq * 10 + 100))
            pkt_data = struct.pack("<H", seq) + chunk
            port.feed(make_device_packet(RSP_DATA, pkt_data))
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        received = bytearray()
        packets = 0
        while True:
            cmd, data = await recv_packet(transport, timeout=1.0)
            if cmd == RSP_DATA:
                seq = struct.unpack("<H", data[:2])[0]
                assert seq == packets
                received.extend(data[2:])
                packets += 1
            elif cmd == RSP_ACK:
                assert data[0] == ACK_OK
                break

        assert packets == 3
        assert len(received) == 300

    @pytest.mark.asyncio
    async def test_ready_interleaved_with_data(self):
        """READY packets during data stream should be skippable."""
        port = FakePort()
        transport = FakeTransport(port)

        port.feed(make_device_packet(RSP_DATA, struct.pack("<H", 0) + b"\xAA" * 100))
        port.feed(make_device_packet(RSP_READY, b"DEFIB"))
        port.feed(make_device_packet(RSP_DATA, struct.pack("<H", 1) + b"\xBB" * 100))
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        received = bytearray()
        packets = 0
        while True:
            cmd, data = await recv_packet(transport, timeout=1.0)
            if cmd == RSP_READY:
                continue
            elif cmd == RSP_DATA:
                received.extend(data[2:])
                packets += 1
            elif cmd == RSP_ACK:
                break

        assert packets == 2
        assert len(received) == 200


# ---------------------------------------------------------------------------
# Tests: CRC32 command roundtrip
# ---------------------------------------------------------------------------

class TestCRC32Packet:
    def test_crc32_response_parse(self):
        """CRC32 response carries 4-byte LE value."""
        crc_val = 0xDEADBEEF
        pkt = make_device_packet(RSP_CRC32, struct.pack("<I", crc_val))
        cmd, data = parse_packet(pkt[:-1])
        assert cmd == RSP_CRC32
        assert struct.unpack("<I", data)[0] == crc_val


# ---------------------------------------------------------------------------
# Tests: SELFUPDATE command
# ---------------------------------------------------------------------------

class TestSelfUpdate:
    def test_selfupdate_packet_format(self):
        """SELFUPDATE command includes addr + size + expected CRC32."""
        import zlib
        addr = 0x41000000
        firmware = b"\xAA" * 256
        expected_crc = zlib.crc32(firmware) & 0xFFFFFFFF
        payload = struct.pack("<III", addr, len(firmware), expected_crc)
        pkt = build_packet(CMD_SELFUPDATE, payload)

        cmd, data = parse_packet(pkt[:-1])
        assert cmd == CMD_SELFUPDATE
        a, s, c = struct.unpack("<III", data)
        assert a == addr
        assert s == 256
        assert c == expected_crc

    @pytest.mark.asyncio
    async def test_selfupdate_sends_correct_payload(self):
        """Host sends SELFUPDATE with addr, size, CRC32."""
        import zlib
        port = FakePort()
        transport = FakeTransport(port)

        firmware = bytes(range(128)) * 2  # 256 bytes
        addr = 0x41000000
        expected_crc = zlib.crc32(firmware) & 0xFFFFFFFF
        payload = struct.pack("<III", addr, len(firmware), expected_crc)

        await send_packet(transport, CMD_SELFUPDATE, payload)
        tx = port.tx_data
        cmd, data = parse_packet(tx[:-1])
        assert cmd == CMD_SELFUPDATE
        a, s, c = struct.unpack("<III", data)
        assert a == addr
        assert s == len(firmware)
        assert c == expected_crc

    @pytest.mark.asyncio
    async def test_selfupdate_data_transfer(self):
        """Simulate full SELFUPDATE: command → ACK → data packets → ACK."""
        port = FakePort()
        transport = FakeTransport(port)

        # Simulate device ACK (ready to receive)
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        cmd, data = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK
        assert data[0] == ACK_OK

        # Send data packets
        firmware = b"\xBB" * 300
        offset = 0
        seq = 0
        while offset < len(firmware):
            chunk = min(100, len(firmware) - offset)
            pkt = struct.pack("<H", seq) + firmware[offset:offset+chunk]
            await send_packet(transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        assert seq == 3
        # Verify all data packets were sent
        assert len(port.tx_data) > 0

    @pytest.mark.asyncio
    async def test_selfupdate_crc_mismatch_returns_error(self):
        """Device should NAK with CRC_ERROR if verification fails."""
        port = FakePort()
        transport = FakeTransport(port)

        # Simulate device rejecting bad CRC
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_CRC_ERROR])))

        cmd, data = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK
        assert data[0] == ACK_CRC_ERROR

    @pytest.mark.asyncio
    async def test_selfupdate_success_flow(self):
        """Full success flow: ACK(ready) → data → ACK(ok)."""
        port = FakePort()
        transport = FakeTransport(port)

        # Device: ACK ready, then after data, ACK ok
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        # Read first ACK (ready)
        cmd, data = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and data[0] == ACK_OK

        # Send some data
        await send_packet(transport, RSP_DATA, struct.pack("<H", 0) + b"\xFF" * 100)

        # Read final ACK (verified)
        cmd, data = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and data[0] == ACK_OK


# ---------------------------------------------------------------------------
# Tests: WRITE command (host→device data transfer)
# ---------------------------------------------------------------------------

class TestWrite:
    def test_write_packet_format(self):
        """WRITE command includes addr + size + expected CRC32."""
        import zlib
        addr = 0x40200000
        data = b"\xAA" * 4096
        expected_crc = zlib.crc32(data) & 0xFFFFFFFF
        payload = struct.pack("<III", addr, len(data), expected_crc)
        pkt = build_packet(CMD_WRITE, payload)

        cmd, parsed = parse_packet(pkt[:-1])
        assert cmd == CMD_WRITE
        a, s, c = struct.unpack("<III", parsed)
        assert a == addr
        assert s == 4096
        assert c == expected_crc

    @pytest.mark.asyncio
    async def test_write_success_flow(self):
        """Full WRITE: command → ACK(ready) → data packets → ACK(ok)."""
        port = FakePort()
        transport = FakeTransport(port)

        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))  # ready
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))  # verified

        # Send WRITE command
        import zlib
        data = bytes(range(256)) * 4  # 1024 bytes
        crc = zlib.crc32(data) & 0xFFFFFFFF
        payload = struct.pack("<III", 0x40200000, len(data), crc)
        await send_packet(transport, CMD_WRITE, payload)

        # Read ACK (ready)
        cmd, resp = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and resp[0] == ACK_OK

        # Send data
        offset = 0
        seq = 0
        while offset < len(data):
            chunk = min(1022, len(data) - offset)
            pkt = struct.pack("<H", seq) + data[offset:offset+chunk]
            await send_packet(transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        # Read final ACK
        cmd, resp = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and resp[0] == ACK_OK

    @pytest.mark.asyncio
    async def test_write_crc_mismatch_rejected(self):
        """WRITE with bad CRC should be rejected, agent stays alive."""
        port = FakePort()
        transport = FakeTransport(port)

        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))       # ready
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_CRC_ERROR])))  # bad CRC

        cmd, resp = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and resp[0] == ACK_OK

        # Send garbage data
        await send_packet(transport, RSP_DATA, struct.pack("<H", 0) + b"\x00" * 100)

        cmd, resp = await recv_packet(transport, timeout=1.0)
        assert cmd == RSP_ACK and resp[0] == ACK_CRC_ERROR


class TestMembw:
    """End-to-end tests for FlashAgentClient.membw via MockTransport."""

    def _membw_response(
        self,
        addr: int = 0x40400000,
        size: int = 4 * 1024 * 1024,
        iters: int = 8,
        timer_hz: int = 1_000_000_000,
        memset_ticks: int = 8_000_000,
        read_ticks: int = 60_000_000,
        memcpy_ticks: int = 16_000_000,
        cpu_arch: int = 1,
    ) -> bytes:
        from defib.agent.protocol import RSP_MEMBW
        payload = struct.pack(
            "<IIIIIIII",
            addr, size, iters, timer_hz,
            memset_ticks, read_ticks, memcpy_ticks, cpu_arch,
        )
        return make_device_packet(RSP_MEMBW, payload)

    @pytest.mark.asyncio
    async def test_parses_response_fields(self):
        from defib.transport.mock import MockTransport
        from defib.agent.client import FlashAgentClient
        from defib.agent.protocol import CMD_MEMBW, parse_packet

        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(make_device_packet(RSP_READY, b"DEFIB"))

        client = FlashAgentClient(t)
        assert await client.connect(timeout=1.0)

        # wait_for_ready drains leftover after parsing READY, so queue
        # the membw response only once the client is connected.
        t.enqueue_rx(self._membw_response())

        result = await client.membw(size_bytes=4 * 1024 * 1024, iters=8)
        assert result.addr == 0x40400000
        assert result.size_bytes == 4 * 1024 * 1024
        assert result.iters == 8
        assert result.timer_hz == 1_000_000_000
        assert result.memset_ticks == 8_000_000
        assert result.read_ticks == 60_000_000
        assert result.memcpy_ticks == 16_000_000

        # Request packet went out with the right opcode and payload shape.
        sent = t.all_tx_data
        frame = sent.rstrip(b"\x00").split(b"\x00")[-1]
        cmd, data = parse_packet(frame)
        assert cmd == CMD_MEMBW
        size, iters, addr = struct.unpack("<III", data)
        assert size == 4 * 1024 * 1024
        assert iters == 8
        assert addr == 0

    @pytest.mark.asyncio
    async def test_mbps_and_cycles_per_byte(self):
        from defib.transport.mock import MockTransport
        from defib.agent.client import FlashAgentClient

        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(make_device_packet(RSP_READY, b"DEFIB"))
        client = FlashAgentClient(t)
        assert await client.connect(timeout=1.0)

        t.enqueue_rx(self._membw_response(
            size=4 * 1024 * 1024,
            iters=8,
            timer_hz=1_000_000_000,
            memset_ticks=8_000_000,
            read_ticks=60_000_000,
            memcpy_ticks=16_000_000,
        ))
        r = await client.membw()

        # 4 MiB × 8 iters = 33,554,432 bytes total. 8,000,000 ticks at
        # 1 GHz = 0.008 s. → 33554432 / 0.008 / 1e6 ≈ 4194.3 MB/s.
        assert r.cycles_per_byte(r.memset_ticks) == pytest.approx(
            8_000_000 / 33_554_432, rel=1e-9
        )
        mbps = r.mbps(r.memset_ticks)
        assert mbps is not None
        assert mbps == pytest.approx(33_554_432 / 0.008 / 1e6, rel=1e-6)

        # memcpy write amplification = 2× (R+W traffic).
        mbps_cpy = r.mbps(r.memcpy_ticks, write_amp=2)
        assert mbps_cpy == pytest.approx(
            2 * 33_554_432 / (16_000_000 / 1_000_000_000) / 1e6, rel=1e-6
        )

    @pytest.mark.asyncio
    async def test_timer_hz_zero_means_mbps_unavailable(self):
        from defib.transport.mock import MockTransport
        from defib.agent.client import FlashAgentClient

        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(make_device_packet(RSP_READY, b"DEFIB"))
        client = FlashAgentClient(t)
        assert await client.connect(timeout=1.0)

        t.enqueue_rx(self._membw_response(timer_hz=0))
        r = await client.membw()

        assert r.timer_hz == 0
        assert r.mbps(r.memset_ticks) is None
        # cycles/byte is still meaningful (CPU-clock-invariant)
        assert r.cycles_per_byte(r.memset_ticks) > 0

    @pytest.mark.asyncio
    async def test_armv5_rejection_raises(self):
        """ARMv5 agent (or invalid params) returns RSP_ACK with FLASH_ERROR.
        Host should raise a clear error, not silently fall through."""
        from defib.agent.protocol import ACK_OK as _ACK_OK  # noqa: F401
        from defib.transport.mock import MockTransport
        from defib.agent.client import FlashAgentClient
        from defib.agent.protocol import RSP_ACK as _RSP_ACK

        t = MockTransport(flush_clears_buffer=False)
        t.enqueue_rx(make_device_packet(RSP_READY, b"DEFIB"))
        client = FlashAgentClient(t)
        assert await client.connect(timeout=1.0)

        t.enqueue_rx(make_device_packet(_RSP_ACK, bytes([0x02])))  # ACK_FLASH_ERROR
        with pytest.raises(RuntimeError, match="rejected"):
            await client.membw()
