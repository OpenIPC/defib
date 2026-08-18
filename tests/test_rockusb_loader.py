"""Tests for RKBOOT loader container parsing.

Entries are read backwards from their stride because ``emType`` is a C enum of
ambiguous width. The parametrised stride tests below are what make that
approach worth having.
"""

import struct

import pytest

from defib.rockusb.loader import (
    LoaderFormatError,
    parse_loader,
    raw_blobs,
)

# uiTag[4] usSize[2] dwVersion[4] dwMergeVersion[4] RKTIME[7] emSupportChip[4]
TABLE_AT = 25


def _build_loader(
    ddr: bytes = b"\xdd" * 32,
    usbplug: bytes = b"\xbb" * 64,
    *,
    entry_size: int = 57,
    rc4_flag: int = 1,
    ddr_delay: int = 0,
    magic: bytes = b"BOOT",
) -> bytes:
    """Synthesise a minimal but structurally honest RKBOOT container."""
    header_len = TABLE_AT + 6 * 3 + 2 + 57
    off471 = header_len
    off472 = off471 + entry_size
    blob_at = off472 + entry_size

    out = bytearray(blob_at)
    out[0:4] = magic
    struct.pack_into("<H", out, 4, header_len)
    struct.pack_into("<BIB", out, TABLE_AT, 1, off471, entry_size)
    struct.pack_into("<BIB", out, TABLE_AT + 6, 1, off472, entry_size)
    struct.pack_into("<BIB", out, TABLE_AT + 12, 0, 0, entry_size)
    out[TABLE_AT + 18 + 1] = rc4_flag  # ucSignFlag then ucRc4Flag

    def put_entry(base: int, name: str, data: bytes, delay: int) -> None:
        out[base] = entry_size
        name_at = base + entry_size - 12 - 40
        out[name_at : name_at + 40] = name.encode("utf-16-le").ljust(40, b"\x00")
        struct.pack_into("<III", out, base + entry_size - 12, len(out), len(data), delay)
        out.extend(data)

    put_entry(off471, "DDR", ddr, ddr_delay)
    put_entry(off472, "USB", usbplug, 0)
    return bytes(out)


class TestParseLoader:
    def test_extracts_both_blobs(self):
        blobs = parse_loader(_build_loader())
        assert blobs.ddr[0].data == b"\xdd" * 32
        assert blobs.usbplug[0].data == b"\xbb" * 64

    def test_extracts_names(self):
        blobs = parse_loader(_build_loader())
        assert blobs.ddr[0].name == "DDR"
        assert blobs.usbplug[0].name == "USB"

    def test_extracts_delay(self):
        blobs = parse_loader(_build_loader(ddr_delay=250))
        assert blobs.ddr[0].delay_ms == 250

    @pytest.mark.parametrize("entry_size", [54, 57, 60])
    def test_stride_independent(self, entry_size):
        """Whatever width the producing toolchain gave the enum, the three
        fields we need sit at the end of the entry."""
        blobs = parse_loader(_build_loader(entry_size=entry_size))
        assert blobs.ddr[0].data == b"\xdd" * 32
        assert blobs.usbplug[0].data == b"\xbb" * 64

    def test_ldr_magic_also_accepted(self):
        blobs = parse_loader(_build_loader(magic=b"LDR "))
        assert blobs.ddr[0].data == b"\xdd" * 32


class TestRc4Flag:
    def test_flag_set_means_rc4_disabled(self):
        blobs = parse_loader(_build_loader(rc4_flag=1))
        assert blobs.rc4_disabled is True
        assert blobs.use_rc4 is False

    def test_flag_clear_means_rc4_required(self):
        blobs = parse_loader(_build_loader(rc4_flag=0))
        assert blobs.rc4_disabled is False
        assert blobs.use_rc4 is True


class TestRejections:
    def test_bare_rkbin_image_rejected_with_a_useful_hint(self):
        """rv1106_usbplug_*.bin has no container — the error should say so
        rather than leaving the caller to guess."""
        bare = b"\x00\x00\xa0\xe1SRKA" + b"\x00" * 200
        with pytest.raises(LoaderFormatError, match="raw_blobs"):
            parse_loader(bare)

    def test_too_short_rejected(self):
        with pytest.raises(LoaderFormatError, match="too short"):
            parse_loader(b"BOOT")

    def test_blob_past_end_rejected(self):
        data = bytearray(_build_loader())
        off471 = struct.unpack_from("<I", data, TABLE_AT + 1)[0]
        struct.pack_into("<I", data, off471 + 57 - 8, 0xFFFF)  # dwDataSize
        with pytest.raises(LoaderFormatError, match="past end"):
            parse_loader(bytes(data))

    def test_entry_past_end_rejected(self):
        data = bytearray(_build_loader())
        struct.pack_into("<I", data, TABLE_AT + 1, 0xFFFF)  # dw471EntryOffset
        with pytest.raises(LoaderFormatError, match="past end"):
            parse_loader(bytes(data))

    def test_absurd_entry_size_rejected(self):
        data = bytearray(_build_loader())
        data[TABLE_AT + 5] = 8  # uc471EntrySize
        with pytest.raises(LoaderFormatError, match="too small"):
            parse_loader(bytes(data))


class TestRawBlobs:
    def test_wraps_bare_images(self):
        blobs = raw_blobs(b"\x01" * 16, b"\x02" * 32)
        assert blobs.ddr[0].data == b"\x01" * 16
        assert blobs.usbplug[0].data == b"\x02" * 32

    def test_defaults_to_rc4_off(self):
        assert raw_blobs(b"a", b"b").use_rc4 is False

    def test_rc4_can_be_forced_on(self):
        assert raw_blobs(b"a", b"b", use_rc4=True).use_rc4 is True
