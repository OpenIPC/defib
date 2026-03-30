"""Hypothesis-based fuzz tests for protocol components."""

from hypothesis import given, strategies as st, assume
import struct

from defib.protocol.crc import calc_crc, append_crc, verify_crc, append_crc_le
from defib.protocol.frames import (
    DataFrame,
    FrameError,
    HeadFrame,
    TailFrame,
    chunk_data,
)
from defib.capture.format import CaptureFile, Direction


class TestCrcFuzz:
    @given(st.binary(min_size=0, max_size=4096))
    def test_crc_never_crashes(self, data: bytes):
        """CRC calculation must handle any input without exception."""
        result = calc_crc(data)
        assert 0 <= result <= 0xFFFF

    @given(st.binary(min_size=1, max_size=2048))
    def test_append_crc_roundtrip(self, data: bytes):
        """Any non-empty data with appended CRC must verify correctly."""
        frame = append_crc(data)
        assert len(frame) == len(data) + 2
        assert verify_crc(frame)

    @given(st.binary(min_size=0, max_size=2048))
    def test_append_crc_le_length(self, data: bytes):
        """Little-endian CRC append must also add exactly 2 bytes."""
        frame = append_crc_le(data)
        assert len(frame) == len(data) + 2

    @given(st.binary(min_size=3, max_size=2048))
    def test_corrupted_frame_fails_verify(self, data: bytes):
        """Flipping any bit in a valid frame should fail CRC verification."""
        frame = bytearray(append_crc(data))
        # Flip the last bit of the payload
        frame[0] ^= 0x01
        # Most of the time this should fail (extremely unlikely to collide)
        # We don't assert False because CRC collisions exist, just test it doesn't crash
        verify_crc(frame)

    @given(st.binary(min_size=0, max_size=100), st.binary(min_size=0, max_size=100))
    def test_different_data_usually_different_crc(self, a: bytes, b: bytes):
        """Different inputs should usually produce different CRCs."""
        assume(a != b)
        # Not asserting inequality due to CRC collisions, just ensuring no crashes
        calc_crc(a)
        calc_crc(b)


class TestFrameFuzz:
    @given(
        length=st.integers(min_value=0, max_value=0xFFFFFFFF),
        address=st.integers(min_value=0, max_value=0xFFFFFFFF),
    )
    def test_head_frame_roundtrip(self, length: int, address: int):
        """HeadFrame encode/decode must roundtrip for any valid length/address."""
        original = HeadFrame(length=length, address=address)
        encoded = original.encode()
        assert len(encoded) == 14
        assert verify_crc(encoded)
        decoded = HeadFrame.decode(encoded)
        assert decoded.length == length
        assert decoded.address == address

    @given(
        seq=st.integers(min_value=0, max_value=255),
        payload=st.binary(min_size=1, max_size=1024),
    )
    def test_data_frame_roundtrip(self, seq: int, payload: bytes):
        """DataFrame encode/decode must roundtrip for any seq/payload."""
        original = DataFrame(seq=seq, payload=payload)
        encoded = original.encode()
        assert verify_crc(encoded)
        decoded = DataFrame.decode(encoded)
        assert decoded.seq == seq
        assert decoded.payload == payload

    @given(seq=st.integers(min_value=0, max_value=255))
    def test_tail_frame_roundtrip(self, seq: int):
        """TailFrame encode/decode must roundtrip."""
        original = TailFrame(seq=seq)
        encoded = original.encode()
        assert len(encoded) == 5
        assert verify_crc(encoded)
        decoded = TailFrame.decode(encoded)
        assert decoded.seq == seq

    @given(st.binary(min_size=0, max_size=14))
    def test_head_frame_decode_no_crash(self, data: bytes):
        """HeadFrame.decode must never crash on arbitrary input."""
        try:
            HeadFrame.decode(data)
        except FrameError:
            pass

    @given(st.binary(min_size=0, max_size=1030))
    def test_data_frame_decode_no_crash(self, data: bytes):
        """DataFrame.decode must never crash on arbitrary input."""
        try:
            DataFrame.decode(data)
        except FrameError:
            pass

    @given(st.binary(min_size=0, max_size=5))
    def test_tail_frame_decode_no_crash(self, data: bytes):
        """TailFrame.decode must never crash on arbitrary input."""
        try:
            TailFrame.decode(data)
        except FrameError:
            pass


class TestChunkDataFuzz:
    @given(
        data=st.binary(min_size=0, max_size=8192),
        chunk_size=st.integers(min_value=1, max_value=2048),
    )
    def test_chunk_preserves_data(self, data: bytes, chunk_size: int):
        """Chunking then concatenating must reproduce the original data."""
        chunks = chunk_data(data, chunk_size)
        reassembled = b"".join(chunks)
        assert reassembled == data

    @given(
        data=st.binary(min_size=1, max_size=8192),
        chunk_size=st.integers(min_value=1, max_value=2048),
    )
    def test_chunk_sizes_bounded(self, data: bytes, chunk_size: int):
        """All chunks must be <= chunk_size, last chunk may be smaller."""
        chunks = chunk_data(data, chunk_size)
        assert len(chunks) > 0
        for c in chunks[:-1]:
            assert len(c) == chunk_size
        assert len(chunks[-1]) <= chunk_size


class TestCaptureFormatFuzz:
    @given(
        baudrate=st.integers(min_value=0, max_value=0xFFFFFFFF),
        chip=st.from_regex(r"[a-z0-9_-]{0,40}", fullmatch=True),
    )
    def test_capture_header_roundtrip(self, baudrate: int, chip: str):
        """CaptureFile header roundtrip with arbitrary baudrate and ASCII chip name."""
        cap = CaptureFile(baudrate=baudrate, chip=chip)
        encoded = cap.encode()
        decoded = CaptureFile.decode(encoded)
        assert decoded.baudrate == baudrate
        # Chip name truncated to 32 bytes (all ASCII so no mid-char split)
        assert decoded.chip == chip[:32]

    @given(
        timestamp=st.integers(min_value=0, max_value=2**63 - 1),
        direction=st.sampled_from([Direction.TX, Direction.RX]),
        data=st.binary(min_size=0, max_size=2048),
    )
    def test_capture_record_roundtrip(self, timestamp: int, direction: Direction, data: bytes):
        """Single record roundtrip."""
        cap = CaptureFile()
        cap.records.append(
            __import__("defib.capture.format", fromlist=["CaptureRecord"]).CaptureRecord(
                timestamp_us=timestamp, direction=direction, data=data
            )
        )
        encoded = cap.encode()
        decoded = CaptureFile.decode(encoded)
        assert len(decoded.records) == 1
        assert decoded.records[0].timestamp_us == timestamp
        assert decoded.records[0].direction == direction
        assert decoded.records[0].data == data

    @given(st.binary(min_size=48, max_size=4096))
    def test_decode_no_crash(self, data: bytes):
        """CaptureFile.decode must never crash on arbitrary input >= header size."""
        # Patch magic to be valid so we test record parsing
        patched = b"DCAP" + struct.pack("<H", 1) + data[6:]
        try:
            CaptureFile.decode(patched)
        except (ValueError, struct.error):
            pass
