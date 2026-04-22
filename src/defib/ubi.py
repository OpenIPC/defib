"""Extract UBIFS volume data from raw UBI images.

A raw UBI image (magic ``UBI#`` / 0x55424923) contains physical erase blocks
(PEBs) with EC and VID headers that encode a chip-specific physical-to-logical
block mapping.  Writing such an image with ``nand write`` on a chip with
different bad blocks shifts data and corrupts the UBIFS inside.

This module extracts the logical UBIFS volume data so it can be written back
via U-Boot's ``ubi write`` command, which creates fresh PEB mappings appropriate
for the target chip.
"""

from __future__ import annotations

import struct

UBI_EC_MAGIC = b"UBI#"  # 0x55424923
UBIFS_MAGIC = b"\x31\x18\x10\x06"  # 0x06101831 LE

# UBI EC header: magic(4) version(1) padding(3) ec(8) vid_hdr_offset(4)
# data_offset(4) image_seq(4) padding(32) hdr_crc(4) = 64 bytes
EC_HDR_FMT = ">4sBxxx Q II I 32x I"
EC_HDR_SIZE = 64

# UBI VID header: magic(4) version(1) vol_type(1) copy_flag(1) compat(1)
# vol_id(4) lnum(4) padding(4) data_size(4) used_ebs(4) data_pad(4)
# data_crc(4) padding(4) sqnum(8) padding(12) hdr_crc(4) = 64 bytes
VID_HDR_FMT = ">4s B B B B I I 4x I I I I 4x Q 12x I"
VID_HDR_SIZE = 64
VID_MAGIC = b"UBI!"  # 0x55424921


def is_ubi_image(data: bytes) -> bool:
    """Check if data starts with UBI EC header magic."""
    return len(data) >= 4 and data[:4] == UBI_EC_MAGIC


def is_ubifs_image(data: bytes) -> bool:
    """Check if data starts with UBIFS superblock magic."""
    return len(data) >= 4 and data[:4] == UBIFS_MAGIC


def extract_ubifs(ubi_data: bytes, peb_size: int = 0x20000, vol_id: int = 0) -> bytes:
    """Extract UBIFS volume data from a raw UBI image.

    Parameters
    ----------
    ubi_data:
        Raw UBI image bytes (starts with ``UBI#`` magic).
    peb_size:
        Physical erase block size in bytes.  Default 128KB (typical SPI NAND).
    vol_id:
        UBI volume ID to extract.  Default 0 (first/only volume).

    Returns
    -------
    bytes
        UBIFS image suitable for ``ubi write``.

    Raises
    ------
    ValueError
        If the image is not a valid UBI image or the volume is not found.
    """
    if not is_ubi_image(ubi_data):
        raise ValueError("Not a UBI image (missing UBI# magic)")

    # Collect LEBs: lnum -> data
    lebs: dict[int, bytes] = {}

    num_pebs = len(ubi_data) // peb_size

    for peb_idx in range(num_pebs):
        peb_off = peb_idx * peb_size

        # Check EC header
        if ubi_data[peb_off : peb_off + 4] != UBI_EC_MAGIC:
            continue

        # Parse EC header for this PEB's offsets (they can vary)
        ec = struct.unpack_from(EC_HDR_FMT, ubi_data, peb_off)
        peb_vid_off = ec[3]
        peb_data_off = ec[4]

        # Check VID header
        vid_off = peb_off + peb_vid_off
        if vid_off + VID_HDR_SIZE > len(ubi_data):
            continue
        if ubi_data[vid_off : vid_off + 4] != VID_MAGIC:
            continue

        vid = struct.unpack_from(VID_HDR_FMT, ubi_data, vid_off)
        # Fields: magic(0), version(1), vol_type(2), copy_flag(3), compat(4),
        #         vol_id(5), lnum(6), data_size(7), used_ebs(8), data_pad(9),
        #         data_crc(10), sqnum(11), hdr_crc(12)
        peb_vol_id = vid[5]
        lnum = vid[6]

        if peb_vol_id != vol_id:
            continue

        # Extract LEB data
        d_off = peb_off + peb_data_off
        peb_leb_size = peb_size - peb_data_off
        leb_data = ubi_data[d_off : d_off + peb_leb_size]
        lebs[lnum] = leb_data

    if not lebs:
        raise ValueError(f"No LEBs found for volume {vol_id}")

    # Assemble in LEB order
    max_leb = max(lebs.keys())
    leb_size_actual = len(next(iter(lebs.values())))
    result = bytearray()
    for i in range(max_leb + 1):
        if i in lebs:
            result.extend(lebs[i])
        else:
            # Missing LEB — fill with 0xFF (erased NAND)
            result.extend(b"\xff" * leb_size_actual)

    # Trim trailing 0xFF pages (unmapped LEBs at the end)
    while len(result) > leb_size_actual and result[-leb_size_actual:] == b"\xff" * leb_size_actual:
        result = result[:-leb_size_actual]

    ubifs = bytes(result)
    if not is_ubifs_image(ubifs):
        raise ValueError(
            f"Extracted data does not start with UBIFS magic "
            f"(got {ubifs[:4].hex()}, expected {UBIFS_MAGIC.hex()})"
        )

    return ubifs
