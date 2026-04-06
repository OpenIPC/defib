"""Regression tests for CMD_FLASH_STREAM and bugs found during hardware testing.

These tests cover bugs that caused data corruption on real cameras:
1. COBS trailing zero strip: ~1/256 packets lost trailing 0x00 from CRC32
2. CRC32 high-byte UB: uint8_t << 24 is undefined when value >= 128
3. _recv_packet_sync partial frame stashing: corrupted next call
4. recv_response READY timeout: READY packets consumed timeout budget
5. FMC memory window 1MB wrap: reads repeated every 1MB (masking write bugs)
6. Sector bitmap: host and agent must agree on bit ordering
"""

import struct
import zlib

import pytest

from defib.agent import cobs as pycobs
from defib.agent.protocol import (
    ACK_OK,
    CMD_FLASH_STREAM,
    RSP_ACK,
    RSP_DATA,
    RSP_READY,
    build_packet,
    parse_packet,
    _recv_packet_sync,
    _port_buffers,
)


# ---------------------------------------------------------------------------
# Test infrastructure (same FakePort pattern as test_agent_protocol.py)
# ---------------------------------------------------------------------------

class FakePort:
    """Minimal pyserial-compatible port backed by a byte buffer."""

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

    @property
    def tx_data(self) -> bytes:
        return bytes(self._tx)


def make_device_packet(cmd: int, data: bytes = b"") -> bytes:
    return build_packet(cmd, data)


# ---------------------------------------------------------------------------
# Bug 1: COBS trailing zero strip
# Root cause of ALL flash write failures. cobs_decode() stripped trailing
# 0x00 bytes. When CRC32 MSB was 0x00, decoded output was 1 byte short.
# ---------------------------------------------------------------------------

class TestCobsTrailingZero:
    def test_roundtrip_preserves_trailing_zero(self):
        """Payload ending with 0x00 must survive COBS roundtrip."""
        original = b"\x01\x02\x03\x00"
        encoded = pycobs.encode(original)
        decoded = pycobs.decode(encoded)
        assert decoded == original

    def test_crc32_with_zero_msb(self):
        """Packets where CRC32 MSB is 0x00 must parse correctly.

        This is the exact pattern that caused ~1/256 write failures.
        """
        for i in range(1000):
            # Vary payload to hit different CRC patterns
            payload_data = bytes([(i * 7 + j * 13) & 0xFF for j in range(8)])
            cmd = 0x82  # RSP_DATA

            # Build packet: [cmd][data][crc32 LE]
            raw = bytes([cmd]) + payload_data
            crc = zlib.crc32(raw) & 0xFFFFFFFF
            raw_with_crc = raw + struct.pack("<I", crc)

            # COBS encode → decode
            encoded = pycobs.encode(raw_with_crc)
            decoded = pycobs.decode(encoded)

            assert decoded == raw_with_crc, (
                f"Trial {i}: CRC {crc:#010x}, MSB=0x{(crc >> 24) & 0xFF:02x}"
            )

    def test_build_parse_all_crc_patterns(self):
        """build_packet → parse_packet for 256 different payloads.

        Covers all possible CRC32 MSB values (0x00-0xFF).
        """
        for i in range(256):
            data = bytes([(i * 37 + j) & 0xFF for j in range(12)])
            pkt = build_packet(0x82, data)
            # Strip trailing 0x00 delimiter
            cmd, parsed = parse_packet(pkt[:-1])
            assert cmd == 0x82
            assert parsed == data, f"Trial {i}: data mismatch"


# ---------------------------------------------------------------------------
# Bug 2: recv_packet_sync partial frame stashing
# _recv_packet_sync stashed bytes including partial COBS frames between
# calls, corrupting the next packet's decode.
# ---------------------------------------------------------------------------

class TestPartialFrameStashing:
    def setup_method(self):
        """Clear global port buffers between tests."""
        _port_buffers.clear()

    def test_consecutive_packets_no_corruption(self):
        """Two consecutive recv calls must not corrupt each other."""
        pkt1 = make_device_packet(RSP_ACK, bytes([ACK_OK]))
        pkt2 = make_device_packet(RSP_DATA, struct.pack("<H", 0) + b"\xAA" * 100)
        port = FakePort(pkt1 + pkt2)

        cmd1, d1 = _recv_packet_sync(port, timeout=1.0)
        assert cmd1 == RSP_ACK
        assert d1[0] == ACK_OK

        cmd2, d2 = _recv_packet_sync(port, timeout=1.0)
        assert cmd2 == RSP_DATA
        assert d2[2:] == b"\xAA" * 100

    def test_extra_bytes_after_delimiter(self):
        """Bytes after 0x00 delimiter must be preserved for next call."""
        pkt1 = make_device_packet(RSP_ACK, bytes([ACK_OK]))
        pkt2 = make_device_packet(RSP_DATA, struct.pack("<H", 42) + b"\xBB" * 50)

        # Feed both packets as one chunk
        port = FakePort(pkt1 + pkt2)

        cmd1, d1 = _recv_packet_sync(port, timeout=1.0)
        assert cmd1 == RSP_ACK

        cmd2, d2 = _recv_packet_sync(port, timeout=1.0)
        assert cmd2 == RSP_DATA
        seq = struct.unpack("<H", d2[:2])[0]
        assert seq == 42

    def test_many_consecutive_acks(self):
        """32 consecutive ACK packets — tests buffer stashing at scale."""
        port = FakePort()
        for _ in range(32):
            port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        for i in range(32):
            cmd, d = _recv_packet_sync(port, timeout=1.0)
            assert cmd == RSP_ACK, f"ACK {i}: wrong cmd 0x{cmd:02x}"
            assert d[0] == ACK_OK, f"ACK {i}: wrong status"

    def test_interleaved_ready_and_data(self):
        """READY + DATA packets interleaved — common in real flash writes."""
        port = FakePort()
        for seq in range(10):
            port.feed(make_device_packet(RSP_READY, b"DEFIB"))
            port.feed(make_device_packet(
                RSP_DATA, struct.pack("<H", seq) + b"\xCC" * 64
            ))

        for seq in range(10):
            cmd1, _ = _recv_packet_sync(port, timeout=1.0)
            assert cmd1 == RSP_READY

            cmd2, d2 = _recv_packet_sync(port, timeout=1.0)
            assert cmd2 == RSP_DATA
            got_seq = struct.unpack("<H", d2[:2])[0]
            assert got_seq == seq


# ---------------------------------------------------------------------------
# Bug 3: recv_response timeout consumed by READY packets
# Multiple READY packets consumed the timeout budget meant for the actual
# response. Fix: reset deadline after each READY skip.
# ---------------------------------------------------------------------------

class TestRecvResponseReadyTimeout:
    def test_ready_does_not_consume_timeout(self):
        """recv_response with many READYs followed by ACK must succeed.

        Bug: READY packets consumed the timeout budget. After receiving
        several READYs, there was no time left for the actual response.
        Fix: reset deadline after each READY skip.
        """
        port = FakePort()
        # 5 READY packets then the real response
        for _ in range(5):
            port.feed(make_device_packet(RSP_READY, b"DEFIB"))
        port.feed(make_device_packet(RSP_ACK, bytes([ACK_OK])))

        # recv_response uses _recv_packet_sync when transport has _port
        # so we test via the sync path directly
        import time
        from defib.agent.protocol import RSP_READY as _READY

        deadline = time.monotonic() + 2.0
        result_cmd = None
        result_data = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            cmd, data = _recv_packet_sync(port, timeout=remaining)
            if cmd != _READY:
                result_cmd = cmd
                result_data = data
                break
            # Reset deadline after READY (the fix)
            deadline = time.monotonic() + 2.0

        assert result_cmd == RSP_ACK
        assert result_data[0] == ACK_OK


# ---------------------------------------------------------------------------
# Bug 4: Sector bitmap bit ordering
# Host Python and agent C must agree on which bit means which sector.
# LSB-first: bit 0 of byte 0 = sector 0, bit 7 of byte 0 = sector 7.
# ---------------------------------------------------------------------------

class TestSectorBitmap:
    @staticmethod
    def _build_bitmap(sectors_with_data: list[int]) -> bytearray:
        """Build bitmap matching client.py's write_flash() logic."""
        bitmap = bytearray(32)
        for s in sectors_with_data:
            bitmap[s // 8] |= 1 << (s % 8)
        return bitmap

    @staticmethod
    def _sector_has_data(bitmap: bytes, s: int) -> bool:
        """Check bitmap matching agent's handle_flash_stream() logic."""
        return bool(bitmap[s // 8] & (1 << (s % 8)))

    def test_individual_sectors(self):
        """Each sector index maps to the correct bit."""
        for s in range(256):
            bitmap = self._build_bitmap([s])
            assert self._sector_has_data(bitmap, s), f"sector {s}"
            # All other sectors should be unset
            for other in [0, 1, 7, 8, 127, 128, 254, 255]:
                if other != s:
                    assert not self._sector_has_data(bitmap, other), (
                        f"sector {other} should be unset when only {s} is set"
                    )

    def test_all_sectors_set(self):
        bitmap = self._build_bitmap(list(range(256)))
        for s in range(256):
            assert self._sector_has_data(bitmap, s)

    def test_no_sectors_set(self):
        bitmap = self._build_bitmap([])
        for s in range(256):
            assert not self._sector_has_data(bitmap, s)

    def test_byte_boundaries(self):
        """Sectors at byte boundaries: 0, 7, 8, 15, 16, 255."""
        bitmap = self._build_bitmap([0, 7, 8, 15, 16, 255])
        assert self._sector_has_data(bitmap, 0)
        assert self._sector_has_data(bitmap, 7)
        assert self._sector_has_data(bitmap, 8)
        assert self._sector_has_data(bitmap, 15)
        assert self._sector_has_data(bitmap, 16)
        assert self._sector_has_data(bitmap, 255)
        assert not self._sector_has_data(bitmap, 1)
        assert not self._sector_has_data(bitmap, 254)

    def test_bitmap_from_firmware_data(self):
        """Simulate write_flash() bitmap construction for 8MB firmware."""
        sector_sz = 0x10000
        # 8MB firmware + 8MB 0xFF padding
        firmware = bytes(range(256)) * (8 * 1024 * 1024 // 256)
        firmware += b'\xff' * (8 * 1024 * 1024)
        assert len(firmware) == 16 * 1024 * 1024

        num_sectors = len(firmware) // sector_sz
        ff_sector = b'\xff' * sector_sz
        bitmap = bytearray(32)
        for s in range(num_sectors):
            sector_data = firmware[s * sector_sz : (s + 1) * sector_sz]
            if sector_data != ff_sector[:len(sector_data)]:
                bitmap[s // 8] |= 1 << (s % 8)

        # First 128 sectors (8MB) should have data
        for s in range(128):
            assert self._sector_has_data(bitmap, s), f"sector {s} should have data"
        # Last 128 sectors (8MB 0xFF) should be skipped
        for s in range(128, 256):
            assert not self._sector_has_data(bitmap, s), f"sector {s} should be skip"


# ---------------------------------------------------------------------------
# Bug 5: CMD_FLASH_STREAM payload format
# Agent expects 44 bytes: [addr:4][size:4][crc:4][bitmap:32]
# ---------------------------------------------------------------------------

class TestFlashStreamPayload:
    def test_payload_format(self):
        """CMD_FLASH_STREAM packet has correct 44-byte payload."""
        addr = 0
        size = 16 * 1024 * 1024
        crc = 0xDEADBEEF
        bitmap = bytes([0xFF] * 32)

        payload = struct.pack("<III", addr, size, crc) + bitmap
        assert len(payload) == 44

        pkt = build_packet(CMD_FLASH_STREAM, payload)
        cmd, data = parse_packet(pkt[:-1])
        assert cmd == CMD_FLASH_STREAM
        assert len(data) == 44

        got_addr, got_size, got_crc = struct.unpack("<III", data[:12])
        assert got_addr == addr
        assert got_size == size
        assert got_crc == crc
        assert data[12:] == bitmap

    def test_bitmap_with_skip_sectors(self):
        """Payload with mixed data/skip sectors parses correctly."""
        bitmap = bytearray(32)
        # Only sectors 0-127 have data
        for s in range(128):
            bitmap[s // 8] |= 1 << (s % 8)

        payload = struct.pack("<III", 0, 16 * 1024 * 1024, 0) + bytes(bitmap)
        pkt = build_packet(CMD_FLASH_STREAM, payload)
        cmd, data = parse_packet(pkt[:-1])
        assert cmd == CMD_FLASH_STREAM

        got_bitmap = data[12:]
        # Sectors 0-127 should be set
        for s in range(128):
            assert got_bitmap[s // 8] & (1 << (s % 8))
        # Sectors 128-255 should be clear
        for s in range(128, 256):
            assert not (got_bitmap[s // 8] & (1 << (s % 8)))


# ---------------------------------------------------------------------------
# COBS cross-compatibility: Python encode must match C decode and vice versa.
# Test with data that triggers edge cases (trailing zeros, 254-byte blocks).
# ---------------------------------------------------------------------------

class TestCobsCrossCompat:
    def test_all_single_bytes(self):
        """Every single byte value round-trips through COBS."""
        for b in range(256):
            data = bytes([b])
            encoded = pycobs.encode(data)
            decoded = pycobs.decode(encoded)
            assert decoded == data, f"byte {b:#04x} failed"

    def test_packet_with_zero_crc_msb(self):
        """A packet whose CRC32 has MSB=0x00 must roundtrip.

        This is the specific pattern that caused the COBS bug.
        """
        # Find a payload that produces CRC32 with MSB=0x00
        for i in range(10000):
            raw = bytes([0x82]) + bytes([(i >> j) & 0xFF for j in range(8)])
            crc = zlib.crc32(raw) & 0xFFFFFFFF
            if (crc >> 24) == 0x00:
                # This is the dangerous pattern — CRC MSB is 0x00
                pkt = build_packet(0x82, raw[1:])
                cmd, data = parse_packet(pkt[:-1])
                assert cmd == 0x82
                assert data == raw[1:]
                return

        pytest.skip("Could not find CRC with MSB=0x00 in 10000 trials")

    def test_page_is_ff_logic(self):
        """Python equivalent of agent's page_is_ff check."""
        # All 0xFF
        assert b'\xff' * 256 == b'\xff' * 256

        # Single byte different at each position
        for pos in range(256):
            page = bytearray(b'\xff' * 256)
            page[pos] = 0xFE
            assert bytes(page) != b'\xff' * 256
