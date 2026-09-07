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


class TestDumpAtomicity:
    """A failed USB dump must not truncate an existing backup or leave a
    partial file behind: dump-flash streams to a temp file and replaces the
    destination only once the whole transfer lands.
    """

    def _drive(self, monkeypatch, tmp_path, dump_impl):
        import asyncio

        import defib.cli.app as app

        class _Rec:
            async def read_flash_info(self):
                return FlashInfo(
                    sectors=4, block_sectors=1, page_sectors=1, ecc_bits=0,
                    access_time=0, manufacturer=0, flash_mask=0,
                )

            async def dump_image(self, start, sectors, write, on_progress=None):
                return await dump_impl(write)

            def close(self):
                pass

        async def _open(*args, **kwargs):
            return _Rec()

        monkeypatch.setattr(app, "_resolve_usb_loader", lambda *a, **k: object())
        monkeypatch.setattr(app, "_open_usb_target", _open)

        import typer

        target = tmp_path / "backup.bin"
        try:
            asyncio.run(
                app._dump_flash_usb_async(
                    chip="rv1106", output_file=str(target), partition="",
                    ddr="", usbplug="", loader="",
                    wait=1.0, usb_path="", power_cycle=False, output="json",
                )
            )
        except typer.Exit:
            pass  # _usb_fail exits on the failure path; file state is asserted
        return target, tmp_path / "backup.bin.partial"

    def test_failed_dump_keeps_prior_backup_and_leaves_no_partial(
        self, monkeypatch, tmp_path
    ):
        (tmp_path / "backup.bin").write_bytes(b"OLD-GOOD-BACKUP")

        async def dump_impl(write):
            write(b"partial")
            raise RockusbError("read stalled mid-dump")

        target, partial = self._drive(monkeypatch, tmp_path, dump_impl)
        assert target.read_bytes() == b"OLD-GOOD-BACKUP"
        assert not partial.exists()

    def test_successful_dump_replaces_the_target(self, monkeypatch, tmp_path):
        async def dump_impl(write):
            write(b"NEW-DUMP")
            return len(b"NEW-DUMP")

        target, partial = self._drive(monkeypatch, tmp_path, dump_impl)
        assert target.read_bytes() == b"NEW-DUMP"
        assert not partial.exists()
