"""Tests for firmware auto-download module."""

from pathlib import Path

import pytest

from defib.firmware import (
    AVAILABLE_FIRMWARE,
    firmware_url,
    has_firmware,
    get_cache_dir,
    get_cached_path,
    pad_to_size,
)


class TestFirmwareUrl:
    def test_known_chip(self):
        url = firmware_url("hi3516ev300")
        assert url is not None
        assert "hi3516ev300" in url
        assert url.endswith(".bin")

    def test_unknown_chip(self):
        assert firmware_url("nonexistent_chip_xyz") is None

    def test_aliased_chip(self):
        url = firmware_url("hi3518ev201")
        assert url is not None
        assert "hi3518ev200" in url

    def test_url_format(self):
        url = firmware_url("gk7205v200")
        assert url == "https://github.com/OpenIPC/firmware/releases/download/latest/u-boot-gk7205v200-universal.bin"


class TestHasFirmware:
    def test_available_chips(self):
        for chip in ["hi3516ev300", "gk7205v200", "hi3518ev200"]:
            assert has_firmware(chip), f"{chip} should be available"

    def test_unavailable_chips(self):
        for chip in ["hi3716mv300", "hi3751v500", "nonexistent"]:
            assert not has_firmware(chip), f"{chip} should not be available"

    def test_aliased_chip(self):
        assert has_firmware("hi3518ev201")
        assert has_firmware("gk7201v200")


class TestAvailableFirmware:
    def test_at_least_20_chips(self):
        assert len(AVAILABLE_FIRMWARE) >= 20

    def test_common_chips_included(self):
        for chip in ["hi3516ev200", "hi3516ev300", "gk7205v200", "hi3518ev200"]:
            assert chip in AVAILABLE_FIRMWARE


class TestCacheDir:
    def test_returns_path(self):
        d = get_cache_dir()
        assert isinstance(d, Path)
        assert d.exists()
        assert "defib" in str(d)

    def test_cached_path_missing(self):
        assert get_cached_path("nonexistent_chip_xyz") is None


class TestPadToSize:
    def test_pads_short_input_with_ff(self):
        # Issue #73: producer dropped 1 MiB padding; consumer pads now.
        # 199276-byte raw u-boot → 1 MiB partition.
        raw = b"\xde\xad\xbe\xef" * 49819  # 199276 bytes
        padded = pad_to_size(raw, 0x100000)
        assert len(padded) == 0x100000
        assert padded[: len(raw)] == raw
        assert padded[len(raw):] == b"\xff" * (0x100000 - len(raw))

    def test_exact_size_unchanged(self):
        data = b"\x00\x11\x22\x33"
        assert pad_to_size(data, 4) == data

    def test_oversize_raises(self):
        with pytest.raises(ValueError, match="larger than target"):
            pad_to_size(b"\x00" * 10, 4)

    def test_custom_fill_byte(self):
        assert pad_to_size(b"x", 4, fill=0x00) == b"x\x00\x00\x00"

    def test_empty_input(self):
        assert pad_to_size(b"", 3) == b"\xff\xff\xff"
