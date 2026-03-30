"""Tests for frame encoding and decoding."""

import pytest

from defib.protocol.crc import verify_crc
from defib.protocol.frames import (
    DataFrame,
    FrameError,
    HeadFrame,
    TailFrame,
    chunk_data,
)


class TestHeadFrame:
    def test_encode_length(self):
        frame = HeadFrame(length=0x40, address=0x04013000)
        encoded = frame.encode()
        assert len(encoded) == 14

    def test_encode_magic(self):
        frame = HeadFrame(length=0x40, address=0x04013000)
        encoded = frame.encode()
        assert encoded[0:4] == b"\xfe\x00\xff\x01"

    def test_encode_big_endian_length(self):
        frame = HeadFrame(length=0x4F00, address=0x04010500)
        encoded = frame.encode()
        assert encoded[4:8] == b"\x00\x00\x4f\x00"

    def test_encode_big_endian_address(self):
        frame = HeadFrame(length=0x40, address=0x81000000)
        encoded = frame.encode()
        assert encoded[8:12] == b"\x81\x00\x00\x00"

    def test_encode_has_valid_crc(self):
        frame = HeadFrame(length=0x40, address=0x04013000)
        encoded = frame.encode()
        assert verify_crc(encoded)

    def test_decode_roundtrip(self):
        original = HeadFrame(length=0x4F00, address=0x04010500)
        encoded = original.encode()
        decoded = HeadFrame.decode(encoded)
        assert decoded.length == original.length
        assert decoded.address == original.address

    def test_decode_too_short(self):
        with pytest.raises(FrameError, match="too short"):
            HeadFrame.decode(b"\x00" * 10)

    def test_decode_bad_magic(self):
        with pytest.raises(FrameError, match="Invalid HEAD magic"):
            HeadFrame.decode(b"\x00" * 14)

    def test_typical_ddr_step_head(self):
        """HEAD frame for typical DDR step: 64 bytes to 0x04013000."""
        frame = HeadFrame(length=64, address=0x04013000)
        encoded = frame.encode()
        assert len(encoded) == 14
        assert verify_crc(encoded)


class TestDataFrame:
    def test_encode_magic(self):
        frame = DataFrame(seq=1, payload=b"\x01\x02\x03")
        encoded = frame.encode()
        assert encoded[0] == 0xDA

    def test_encode_sequence(self):
        frame = DataFrame(seq=5, payload=b"\x00")
        encoded = frame.encode()
        assert encoded[1] == 5
        assert encoded[2] == (~5) & 0xFF

    def test_encode_has_crc(self):
        frame = DataFrame(seq=1, payload=b"hello")
        encoded = frame.encode()
        assert verify_crc(encoded)

    def test_decode_roundtrip(self):
        payload = bytes(range(64))
        original = DataFrame(seq=3, payload=payload)
        encoded = original.encode()
        decoded = DataFrame.decode(encoded)
        assert decoded.seq == 3
        assert decoded.payload == payload

    def test_max_payload(self):
        """1024-byte payload (MAX_DATA_LEN)."""
        payload = bytes(range(256)) * 4
        frame = DataFrame(seq=1, payload=payload)
        encoded = frame.encode()
        assert verify_crc(encoded)
        decoded = DataFrame.decode(encoded)
        assert decoded.payload == payload

    def test_decode_too_short(self):
        with pytest.raises(FrameError, match="too short"):
            DataFrame.decode(b"\xda\x01")

    def test_decode_bad_magic(self):
        with pytest.raises(FrameError, match="Invalid DATA magic"):
            DataFrame.decode(b"\xff\x01\xfe\x00\x00\x00")

    def test_sequence_inversion(self):
        """Sequence byte and its complement must match."""
        frame = DataFrame(seq=0x42, payload=b"\x00")
        encoded = frame.encode()
        assert encoded[1] == 0x42
        assert encoded[2] == 0xBD  # ~0x42 & 0xFF


class TestTailFrame:
    def test_encode_length(self):
        frame = TailFrame(seq=2)
        encoded = frame.encode()
        assert len(encoded) == 5

    def test_encode_magic(self):
        frame = TailFrame(seq=2)
        encoded = frame.encode()
        assert encoded[0] == 0xED

    def test_encode_has_crc(self):
        frame = TailFrame(seq=10)
        encoded = frame.encode()
        assert verify_crc(encoded)

    def test_decode_roundtrip(self):
        original = TailFrame(seq=7)
        encoded = original.encode()
        decoded = TailFrame.decode(encoded)
        assert decoded.seq == 7

    def test_decode_too_short(self):
        with pytest.raises(FrameError, match="too short"):
            TailFrame.decode(b"\xed\x01")


class TestChunkData:
    def test_exact_chunk(self):
        data = b"\x00" * 1024
        chunks = chunk_data(data, 1024)
        assert len(chunks) == 1
        assert chunks[0] == data

    def test_multiple_chunks(self):
        data = b"\x00" * 2500
        chunks = chunk_data(data, 1024)
        assert len(chunks) == 3
        assert len(chunks[0]) == 1024
        assert len(chunks[1]) == 1024
        assert len(chunks[2]) == 452

    def test_small_data(self):
        data = b"\x01\x02\x03"
        chunks = chunk_data(data, 1024)
        assert len(chunks) == 1
        assert chunks[0] == data

    def test_empty_data(self):
        chunks = chunk_data(b"", 1024)
        assert len(chunks) == 0
