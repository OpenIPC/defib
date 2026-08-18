"""Tests for rockusb CBW/CSW framing.

The wrapper is little-endian but the command block inside it is big-endian.
Most of these tests exist to pin that down.
"""

import struct

import pytest

from defib.rockusb.protocol import (
    CBW_LENGTH,
    CBW_SIGNATURE,
    CSW_LENGTH,
    CSW_SIGNATURE,
    DIRECTION_IN,
    DIRECTION_OUT,
    MAX_SECTORS_PER_TRANSFER,
    SECTOR_SIZE,
    Opcode,
    ResetSubcode,
    RockusbError,
    build_cbw,
    parse_csw,
    split_lba_transfers,
)


def _csw(tag: int, residue: int = 0, status: int = 0, signature: bytes = CSW_SIGNATURE):
    return signature + struct.pack("<IIB", tag, residue, status)


class TestBuildCbw:
    def test_length_is_31(self):
        assert len(build_cbw(1, Opcode.TEST_UNIT_READY)) == CBW_LENGTH

    def test_signature_and_tag(self):
        cbw = build_cbw(0xDEADBEEF, Opcode.TEST_UNIT_READY)
        assert cbw[:4] == CBW_SIGNATURE
        assert struct.unpack_from("<I", cbw, 4)[0] == 0xDEADBEEF

    def test_tag_is_masked_to_32_bits(self):
        cbw = build_cbw(0x1_0000_0001, Opcode.TEST_UNIT_READY)
        assert struct.unpack_from("<I", cbw, 4)[0] == 1

    def test_transfer_length_is_little_endian(self):
        cbw = build_cbw(1, Opcode.WRITE_LBA, count=2)
        assert struct.unpack_from("<I", cbw, 8)[0] == 2 * SECTOR_SIZE

    def test_explicit_transfer_length_wins(self):
        cbw = build_cbw(1, Opcode.READ_FLASH_ID, transfer_length=5, direction_in=True)
        assert struct.unpack_from("<I", cbw, 8)[0] == 5

    def test_direction_flag(self):
        assert build_cbw(1, Opcode.READ_LBA, count=1, direction_in=True)[12] == DIRECTION_IN
        assert build_cbw(1, Opcode.WRITE_LBA, count=1)[12] == DIRECTION_OUT

    def test_lun_and_cdb_length(self):
        cbw = build_cbw(1, Opcode.TEST_UNIT_READY)
        assert cbw[13] == 0  # LUN
        assert cbw[14] == 16  # CDB length

    def test_opcode_and_subcode_placement(self):
        cbw = build_cbw(1, Opcode.RESET_DEVICE, subcode=int(ResetSubcode.MASKROM))
        assert cbw[15] == Opcode.RESET_DEVICE
        assert cbw[16] == ResetSubcode.MASKROM

    def test_address_is_big_endian(self):
        """The single easiest thing to get wrong — a byte-swapped LBA writes
        to a wildly different place on flash."""
        cbw = build_cbw(1, Opcode.WRITE_LBA, address=0x01020304, count=1)
        assert cbw[17:21] == b"\x01\x02\x03\x04"

    def test_count_is_big_endian(self):
        cbw = build_cbw(1, Opcode.WRITE_LBA, address=0, count=0x0102)
        assert cbw[22:24] == b"\x01\x02"

    def test_trailing_cdb_bytes_are_zero(self):
        cbw = build_cbw(1, Opcode.WRITE_LBA, address=0xFFFFFFFF, count=0xFFFF)
        assert cbw[24:31] == bytes(7)

    def test_accepts_plain_int_opcode(self):
        assert build_cbw(1, 0x14)[15] == 0x14


class TestParseCsw:
    def test_round_trip(self):
        tag, residue, status = parse_csw(_csw(0xCAFEBABE, residue=7, status=0))
        assert (tag, residue, status) == (0xCAFEBABE, 7, 0)

    def test_tag_checked_when_requested(self):
        parse_csw(_csw(42), expected_tag=42)
        with pytest.raises(RockusbError, match="does not match"):
            parse_csw(_csw(42), expected_tag=43)

    def test_tag_compared_modulo_32_bits(self):
        parse_csw(_csw(1), expected_tag=0x1_0000_0001)

    def test_bad_signature_rejected(self):
        with pytest.raises(RockusbError, match="signature"):
            parse_csw(_csw(1, signature=b"NOPE"))

    def test_short_read_rejected(self):
        with pytest.raises(RockusbError, match="short"):
            parse_csw(_csw(1)[:-1])

    def test_failure_status_is_returned_not_raised(self):
        """Framing is this layer's job; deciding what a failed status means is
        the caller's."""
        _, _, status = parse_csw(_csw(1, status=1))
        assert status == 1

    def test_csw_length_constant(self):
        assert len(_csw(1)) == CSW_LENGTH


class TestSplitLbaTransfers:
    def test_single_transfer_when_small(self):
        assert split_lba_transfers(0, 10) == [(0, 10)]

    def test_splits_at_max(self):
        out = split_lba_transfers(0, MAX_SECTORS_PER_TRANSFER + 1)
        assert out == [(0, MAX_SECTORS_PER_TRANSFER), (MAX_SECTORS_PER_TRANSFER, 1)]

    def test_offsets_are_contiguous_and_complete(self):
        out = split_lba_transfers(100, 2000, max_sectors=256)
        assert sum(c for _, c in out) == 2000
        assert out[0][0] == 100
        for (lba_a, count_a), (lba_b, _) in zip(out, out[1:]):
            assert lba_a + count_a == lba_b

    def test_zero_sectors_is_empty(self):
        assert split_lba_transfers(0, 0) == []

    def test_exact_multiple_has_no_remainder_chunk(self):
        out = split_lba_transfers(0, 512, max_sectors=256)
        assert out == [(0, 256), (256, 256)]

    def test_rejects_negative_count(self):
        with pytest.raises(ValueError, match="negative"):
            split_lba_transfers(0, -1)

    def test_rejects_nonpositive_max(self):
        with pytest.raises(ValueError, match="positive"):
            split_lba_transfers(0, 10, max_sectors=0)
