"""Tests for CRC-16/CCITT implementation."""

from defib.protocol.crc import calc_crc, append_crc, append_crc_le, verify_crc


class TestCalcCrc:
    def test_empty_data(self):
        result = calc_crc(b"")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_single_byte(self):
        result = calc_crc(b"\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_deterministic(self):
        data = b"\xfe\x00\xff\x01\x00\x00\x00\x40\x04\x01\x30\x00"
        crc1 = calc_crc(data)
        crc2 = calc_crc(data)
        assert crc1 == crc2

    def test_different_data_different_crc(self):
        assert calc_crc(b"\x01\x02\x03") != calc_crc(b"\x04\x05\x06")

    def test_known_head_frame_magic(self):
        """HEAD frame magic bytes should produce a consistent CRC."""
        magic = bytes([0xFE, 0x00, 0xFF, 0x01])
        crc = calc_crc(magic)
        assert isinstance(crc, int)
        assert crc == calc_crc(bytearray(magic))

    def test_bytearray_input(self):
        data = bytearray([0xDA, 0x01, 0xFE, 0x48, 0x65, 0x6C, 0x6C, 0x6F])
        result = calc_crc(data)
        assert result == calc_crc(bytes(data))

    def test_ack_byte_crc(self):
        """CRC of ACK byte (0xAA)."""
        crc = calc_crc(b"\xaa")
        assert isinstance(crc, int)


class TestAppendCrc:
    def test_appends_two_bytes(self):
        data = b"\xfe\x00\xff\x01"
        result = append_crc(data)
        assert len(result) == len(data) + 2

    def test_preserves_original_data(self):
        data = b"\xfe\x00\xff\x01"
        result = append_crc(data)
        assert result[:4] == data

    def test_verifiable(self):
        data = b"\xfe\x00\xff\x01\x00\x00\x10\x00\x04\x01\x30\x00"
        with_crc = append_crc(data)
        assert verify_crc(with_crc)

    def test_big_endian(self):
        data = b"\x01\x02\x03"
        result = append_crc(data)
        crc = calc_crc(data)
        assert result[-2] == (crc >> 8) & 0xFF
        assert result[-1] == crc & 0xFF


class TestAppendCrcLe:
    def test_little_endian(self):
        data = b"\x01\x02\x03"
        result = append_crc_le(data)
        crc = calc_crc(data)
        assert result[-1] == (crc >> 8) & 0xFF
        assert result[-2] == crc & 0xFF


class TestVerifyCrc:
    def test_valid_frame(self):
        data = b"\xfe\x00\xff\x01"
        frame = append_crc(data)
        assert verify_crc(frame) is True

    def test_corrupted_frame(self):
        data = b"\xfe\x00\xff\x01"
        frame = bytearray(append_crc(data))
        frame[-1] ^= 0xFF  # Corrupt last byte
        assert verify_crc(frame) is False

    def test_too_short(self):
        assert verify_crc(b"\x00\x01") is False
        assert verify_crc(b"\x00") is False
        assert verify_crc(b"") is False

    def test_roundtrip_various_lengths(self):
        for length in [1, 10, 100, 1024]:
            data = bytes(range(256)) * (length // 256 + 1)
            data = data[:length]
            frame = append_crc(data)
            assert verify_crc(frame), f"Failed for length {length}"
