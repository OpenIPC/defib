"""Tests for READ_FLASH_INFO parsing.

The reply is what a dump trusts for "how much is there", so a wrong decode
either truncates the image or runs off the end into stalled reads.
"""

import struct

import pytest

from defib.rockusb.protocol import (
    FLASH_INFO_LENGTH,
    FlashInfo,
    RockusbError,
    parse_flash_info,
)

# Captured from a Luckfox Pico Max (RV1106G3) over rockusb.
REAL = bytes.fromhex("00fc070000010400280001")


class TestParseRealDevice:
    def test_decodes_the_captured_reply(self):
        info = parse_flash_info(REAL)
        assert info.sectors == 523264
        assert info.block_sectors == 256
        assert info.page_sectors == 4

    def test_capacity_is_just_under_the_part_size(self):
        """256 MiB of SPI NAND, less the FTL's own reserve."""
        info = parse_flash_info(REAL)
        assert 255.0 < info.size_bytes / 1024 / 1024 < 256.0

    def test_block_size_matches_the_kernels_erasesize(self):
        """/proc/mtd on the same board reports erasesize 0x20000."""
        assert parse_flash_info(REAL).block_bytes == 0x20000

    def test_page_size_is_2k(self):
        assert parse_flash_info(REAL).page_bytes == 2048

    def test_str_is_readable(self):
        text = str(parse_flash_info(REAL))
        assert "255.5 MiB" in text
        assert "block 128 KiB" in text


class TestRejections:
    def test_short_reply_rejected(self):
        with pytest.raises(RockusbError, match="short flash info"):
            parse_flash_info(REAL[:-1])

    def test_zero_capacity_rejected(self):
        """A loader that is running but never brought flash up would
        otherwise yield a convincing file full of nothing."""
        bad = struct.pack("<IHBBBBB", 0, 256, 4, 0, 40, 0, 1)
        with pytest.raises(RockusbError, match="zero capacity"):
            parse_flash_info(bad)

    def test_exact_length_accepted(self):
        assert len(REAL) == FLASH_INFO_LENGTH
        parse_flash_info(REAL)

    def test_trailing_bytes_ignored(self):
        assert parse_flash_info(REAL + b"\xff" * 4).sectors == 523264


class TestGeometry:
    def test_size_bytes(self):
        assert FlashInfo(2, 1, 1, 0, 0, 0, 0).size_bytes == 1024

    def test_partitions_fit_inside_reported_capacity(self):
        """The rv1106 profile's layout must not run past what the device
        answers for, or a full-partition dump ends in stalled reads."""
        from defib.profiles.loader import load_profile

        info = parse_flash_info(REAL)
        for name, part in load_profile("rv1106").partitions.items():
            assert part.end_lba <= info.sectors, name
