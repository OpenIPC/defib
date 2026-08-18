"""Tests for the Rockchip MaskROM wire codecs (CRC-16 and RC4)."""

import struct

import pytest

from defib.rockusb.codec import CRC16_INIT, RK_RC4_KEY, rc4, rk_crc16


def _crc16_bitwise(data: bytes, crc: int = CRC16_INIT) -> int:
    """Independent bit-by-bit CRC-CCITT, to check the table-driven version.

    Poly 0x1021, MSB-first, no final XOR.
    """
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


class TestRkCrc16:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x00",
            b"\xff",
            b"123456789",
            bytes(range(256)),
            b"\x00" * 4096,
            bytes(range(256)) * 17,
        ],
    )
    def test_matches_bitwise_reference(self, data):
        assert rk_crc16(data) == _crc16_bitwise(data)

    def test_check_vector(self):
        # CRC-16/CCITT-FALSE over b"123456789" is the standard 0x29B1 check
        # value; this pins the seed and bit order, not just self-consistency.
        assert rk_crc16(b"123456789") == 0x29B1

    def test_empty_returns_seed(self):
        assert rk_crc16(b"") == CRC16_INIT

    def test_in_range(self):
        assert 0 <= rk_crc16(bytes(range(256))) <= 0xFFFF

    def test_differs_from_hisilicon_variant(self):
        """Guard against anyone 'simplifying' this to reuse calc_crc().

        The HiSilicon helper seeds at 0 and pads with two zero bytes; feeding a
        Rockchip blob through it would produce a loader the boot ROM rejects.
        """
        from defib.protocol.crc import calc_crc

        data = b"\xde\xad\xbe\xef"
        assert rk_crc16(data) != calc_crc(data)

    def test_bytearray_accepted(self):
        assert rk_crc16(bytearray(b"abc")) == rk_crc16(b"abc")


class TestRc4:
    def test_known_vector(self):
        # RFC 6229-style vector: key "Key", plaintext "Plaintext".
        assert rc4(b"Plaintext", b"Key").hex() == "bbf316e8d940af0ad3"

    def test_second_known_vector(self):
        assert rc4(b"pedia", b"Wiki").hex() == "1021bf0420"

    def test_self_inverse(self):
        plain = bytes(range(256)) * 3
        assert rc4(rc4(plain)) == plain

    def test_default_key_is_the_rockchip_constant(self):
        assert len(RK_RC4_KEY) == 16
        assert RK_RC4_KEY.hex() == "7c4e0304550509072d2c7b38170d1711"

    def test_length_preserved(self):
        assert len(rc4(b"\x00" * 1000)) == 1000

    def test_empty(self):
        assert rc4(b"") == b""


class TestCrcAppendOrder:
    def test_appended_big_endian(self):
        """The boot ROM wants the high byte first."""
        payload = b"\x01\x02\x03"
        crc = rk_crc16(payload)
        packed = struct.pack(">H", crc)
        assert packed[0] == (crc >> 8) & 0xFF
        assert packed[1] == crc & 0xFF
