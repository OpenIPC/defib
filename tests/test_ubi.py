"""Tests for UBI image parsing and UBIFS extraction."""

import struct
import zlib

import pytest

from defib.ubi import (
    UBI_EC_MAGIC,
    UBIFS_MAGIC,
    VID_MAGIC,
    extract_ubifs,
    is_ubi_image,
    is_ubifs_image,
)

PEB_SIZE = 0x20000  # 128KB
VID_HDR_OFFSET = 2048
DATA_OFFSET = 4096
LEB_SIZE = PEB_SIZE - DATA_OFFSET


def _make_ec_header(vid_hdr_offset: int = VID_HDR_OFFSET, data_offset: int = DATA_OFFSET) -> bytes:
    """Build a minimal UBI EC header (64 bytes) with valid CRC."""
    # Pack without CRC first
    hdr = struct.pack(
        ">4sBxxx Q II I 32x",
        UBI_EC_MAGIC,
        1,  # version
        0,  # erase counter
        vid_hdr_offset,
        data_offset,
        1,  # image_seq
    )
    crc = zlib.crc32(hdr[:60]) & 0xFFFFFFFF
    return hdr[:60] + struct.pack(">I", crc)


def _make_vid_header(vol_id: int, lnum: int, data_size: int = 0) -> bytes:
    """Build a minimal UBI VID header (64 bytes) with valid CRC."""
    hdr = struct.pack(
        ">4s B B B B I I 4x I I I I 4x Q 12x",
        VID_MAGIC,
        1,  # version
        1,  # vol_type (dynamic)
        0,  # copy_flag
        0,  # compat
        vol_id,
        lnum,
        data_size,
        0,  # used_ebs
        0,  # data_pad
        0,  # data_crc
        0,  # sqnum
    )
    crc = zlib.crc32(hdr[:60]) & 0xFFFFFFFF
    return hdr[:60] + struct.pack(">I", crc)


def _make_peb(vol_id: int, lnum: int, leb_data: bytes) -> bytes:
    """Build a complete PEB with EC header, VID header, and data."""
    peb = bytearray(PEB_SIZE)
    # Fill with 0xFF (erased NAND)
    for i in range(PEB_SIZE):
        peb[i] = 0xFF

    ec = _make_ec_header()
    peb[0 : len(ec)] = ec

    vid = _make_vid_header(vol_id, lnum, len(leb_data))
    peb[VID_HDR_OFFSET : VID_HDR_OFFSET + len(vid)] = vid

    peb[DATA_OFFSET : DATA_OFFSET + len(leb_data)] = leb_data

    return bytes(peb)


def _make_ubifs_superblock() -> bytes:
    """Make a fake UBIFS superblock (just the magic + padding)."""
    return UBIFS_MAGIC + b"\x00" * (LEB_SIZE - 4)


def test_is_ubi_image():
    assert is_ubi_image(UBI_EC_MAGIC + b"\x00" * 60)
    assert not is_ubi_image(b"\x00\x00\x00\x00")
    assert not is_ubi_image(UBIFS_MAGIC)
    assert not is_ubi_image(b"")


def test_is_ubifs_image():
    assert is_ubifs_image(UBIFS_MAGIC + b"\x00" * 60)
    assert not is_ubifs_image(UBI_EC_MAGIC)
    assert not is_ubifs_image(b"")


def test_extract_single_leb():
    """Extract a single-LEB UBIFS volume."""
    leb_data = _make_ubifs_superblock()
    peb = _make_peb(vol_id=0, lnum=0, leb_data=leb_data)
    ubifs = extract_ubifs(peb, peb_size=PEB_SIZE, vol_id=0)
    assert ubifs[:4] == UBIFS_MAGIC
    assert len(ubifs) == LEB_SIZE


def test_extract_multiple_lebs():
    """Extract a multi-LEB volume with LEBs in arbitrary PEB order."""
    leb0 = _make_ubifs_superblock()
    leb1 = b"\xAA" * LEB_SIZE
    leb2 = b"\xBB" * LEB_SIZE

    # PEBs in reverse order
    image = _make_peb(0, 2, leb2) + _make_peb(0, 0, leb0) + _make_peb(0, 1, leb1)
    ubifs = extract_ubifs(image, peb_size=PEB_SIZE, vol_id=0)

    assert ubifs[:4] == UBIFS_MAGIC
    assert ubifs[LEB_SIZE : LEB_SIZE + 1] == b"\xAA"
    assert ubifs[2 * LEB_SIZE : 2 * LEB_SIZE + 1] == b"\xBB"
    assert len(ubifs) == 3 * LEB_SIZE


def test_extract_filters_by_vol_id():
    """Only extract the requested volume ID."""
    leb_vol0 = _make_ubifs_superblock()
    leb_vol1 = b"\xCC" * LEB_SIZE

    image = _make_peb(0, 0, leb_vol0) + _make_peb(1, 0, leb_vol1)
    ubifs = extract_ubifs(image, peb_size=PEB_SIZE, vol_id=0)
    assert ubifs[:4] == UBIFS_MAGIC


def test_extract_missing_volume_raises():
    """Raise ValueError if requested volume ID not found."""
    leb = _make_ubifs_superblock()
    image = _make_peb(0, 0, leb)
    with pytest.raises(ValueError, match="No LEBs found"):
        extract_ubifs(image, peb_size=PEB_SIZE, vol_id=99)


def test_extract_not_ubi_raises():
    """Raise ValueError for non-UBI data."""
    with pytest.raises(ValueError, match="Not a UBI image"):
        extract_ubifs(b"\x00" * PEB_SIZE, peb_size=PEB_SIZE)


def test_extract_skips_empty_pebs():
    """PEBs without VID header (erased) are skipped."""
    leb = _make_ubifs_superblock()
    good_peb = _make_peb(0, 0, leb)

    # Erased PEB: EC header but no VID
    erased = bytearray(PEB_SIZE)
    for i in range(PEB_SIZE):
        erased[i] = 0xFF
    ec = _make_ec_header()
    erased[0 : len(ec)] = ec

    image = bytes(erased) + good_peb
    ubifs = extract_ubifs(image, peb_size=PEB_SIZE, vol_id=0)
    assert ubifs[:4] == UBIFS_MAGIC
