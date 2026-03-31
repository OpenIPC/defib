"""Tests for COBS encoding/decoding and agent protocol."""

import struct
import zlib

from defib.agent.cobs import decode, encode
from defib.agent.protocol import build_packet, parse_packet, CMD_INFO, CMD_READ, RSP_READY


class TestCOBSEncode:
    def test_no_zeros(self):
        """Data without zeros should be encoded with minimal overhead."""
        data = bytes([1, 2, 3, 4, 5])
        encoded = encode(data)
        assert 0x00 not in encoded
        assert len(encoded) <= len(data) + 2

    def test_single_zero(self):
        data = bytes([0x00])
        encoded = encode(data)
        assert 0x00 not in encoded

    def test_all_zeros(self):
        data = bytes([0, 0, 0, 0])
        encoded = encode(data)
        assert 0x00 not in encoded

    def test_zero_in_middle(self):
        data = bytes([1, 2, 0, 3, 4])
        encoded = encode(data)
        assert 0x00 not in encoded

    def test_empty(self):
        encoded = encode(b"")
        assert 0x00 not in encoded

    def test_254_non_zero(self):
        """Block of 254 non-zero bytes should produce 0xFF marker."""
        data = bytes(range(1, 255))
        encoded = encode(data)
        assert 0x00 not in encoded

    def test_large_data(self):
        data = bytes(range(256)) * 4  # 1024 bytes with zeros
        encoded = encode(data)
        assert 0x00 not in encoded


class TestCOBSDecode:
    def test_roundtrip_no_zeros(self):
        data = bytes([1, 2, 3, 4, 5])
        assert decode(encode(data)) == data

    def test_roundtrip_with_zeros(self):
        data = bytes([1, 0, 2, 0, 3])
        assert decode(encode(data)) == data

    def test_roundtrip_all_zeros(self):
        data = bytes([0, 0, 0, 0])
        assert decode(encode(data)) == data

    def test_roundtrip_empty(self):
        assert decode(encode(b"")) == b""

    def test_roundtrip_single_byte(self):
        for b in range(256):
            data = bytes([b])
            assert decode(encode(data)) == data

    def test_roundtrip_large(self):
        data = bytes(range(256)) * 4
        assert decode(encode(data)) == data

    def test_roundtrip_flash_data(self):
        """Simulate flash data: mix of 0xFF runs and random data."""
        data = b"\xff" * 256 + bytes(range(128)) + b"\xff" * 512 + b"\x00" * 64
        assert decode(encode(data)) == data


class TestAgentProtocol:
    def test_build_parse_roundtrip(self):
        cmd, data = parse_packet(encode(
            bytes([CMD_INFO]) + struct.pack("<I", zlib.crc32(bytes([CMD_INFO])) & 0xFFFFFFFF)
        ))
        # Direct packet build/parse
        pkt = build_packet(CMD_INFO)
        # Remove trailing 0x00 delimiter for parse
        raw = pkt[:-1]
        cmd, data = parse_packet(raw)
        assert cmd == CMD_INFO
        assert data == b""

    def test_build_read_command(self):
        addr = 0x00000000
        size = 0x1000
        payload = struct.pack("<II", addr, size)
        pkt = build_packet(CMD_READ, payload)
        assert pkt[-1:] == b"\x00"  # Delimiter

        cmd, data = parse_packet(pkt[:-1])
        assert cmd == CMD_READ
        assert struct.unpack("<II", data) == (0, 0x1000)

    def test_crc_mismatch_detected(self):
        pkt = build_packet(CMD_INFO)
        raw = bytearray(pkt[:-1])
        # Corrupt a byte
        if len(raw) > 2:
            raw[1] ^= 0xFF
        import pytest
        with pytest.raises(ValueError, match="CRC"):
            parse_packet(bytes(raw))

    def test_ready_packet(self):
        """Agent sends READY with 'DEFIB' marker."""
        pkt = build_packet(RSP_READY, b"DEFIB")
        cmd, data = parse_packet(pkt[:-1])
        assert cmd == RSP_READY
        assert data == b"DEFIB"
