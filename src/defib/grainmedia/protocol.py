"""GM8126 USB framing: command blocks, word order, the flash header.

Plain USB mass-storage wrappers (31-byte CBW, 13-byte CSW) around a vendor
CDB that starts ``F0 <command>``. Parameters are big-endian. Nothing but the
transport protects the payload, so callers read back what they write.

Pure functions, so all of it is testable without a device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

GRAINMEDIA_VID = 0x1233

CBW_SIGNATURE = b"USBC"
CSW_SIGNATURE = b"USBS"
CBW_LENGTH = 31
CSW_LENGTH = 13

INQUIRY_LENGTH = 36
#: The vendor tool accepts a device on these bytes alone: the INQUIRY header
#: and the first letter of the vendor field.
INQUIRY_PREFIX = bytes.fromhex("008002021f000000") + b"G"

FLASH_ID_LENGTH = 5
#: What ``F0 1E`` answers when the device is strapped for SPI NOR. It is a
#: marker, not a JEDEC id; a NAND id here means a NAND board.
FLASH_ID_SPI = bytes.fromhex("ff00ff00ff")

PAGE_LOG2 = 8
PAGE_SIZE = 1 << PAGE_LOG2
#: ``F0 23`` issues opcode D8, whatever sector size the geometry names.
ERASE_SIZE = 0x10000
ERASE_LOG2 = 16
#: 24-bit addresses: the device puts ``address`` in the low bits of the
#: opcode word and has no bank or 4-byte command.
MAX_FLASH_SIZE = 0x1000000

#: The SPI clock divider fsm.bin itself starts with.
SPI_DIVIDER = 4

#: The system header, page 0 of flash, as the SoC reads it.
HEADER_MAGIC = b"GM8126\x00\x00"
HEADER_END = b"\x55\xaa"
IMAGE_MAGIC = 0x805A474D
#: fsm.bin loads this much of the first-stage loader, whatever its header says.
NSBOOT_LOAD_SIZE = 0x5000


class GrainMediaError(Exception):
    """Protocol or device failure during GM8126 USB recovery."""


@dataclass(frozen=True)
class Command:
    """One command block, its data direction and length."""

    cdb: bytes
    length: int = 0
    direction_in: bool = False


def inquiry() -> Command:
    return Command(bytes([0x12, 0, 0, 0, INQUIRY_LENGTH, 0]), INQUIRY_LENGTH, True)


def read_flash_id() -> Command:
    return Command(bytes([0xF0, 0x1E, 0, 0, 0, 0]), FLASH_ID_LENGTH, True)


def set_spi_geometry(chip_log2: int) -> Command:
    return Command(bytes([0xF0, 0x25, SPI_DIVIDER, PAGE_LOG2, ERASE_LOG2, chip_log2]))


def erase_block(address: int) -> Command:
    """Erase one 64 KiB block. ``address`` is in bytes, unlike the page commands.

    The count stays 1, as in the vendor tool: the device's loop adds 1 to the
    byte address per block, so a higher count erases the same block again.
    """
    if address % ERASE_SIZE:
        raise ValueError(f"erase address {address:#x} is not 64 KiB aligned")
    if not 0 <= address < MAX_FLASH_SIZE:
        raise ValueError(f"erase address {address:#x} is outside the flash")
    return Command(struct.pack(">BBIHBB", 0xF0, 0x23, address, 1, 0, 0))


def program_page(page: int) -> Command:
    """Program one page. The device takes exactly one page per command."""
    return Command(struct.pack(">BBII", 0xF0, 0x21, page, 0), PAGE_SIZE)


def read_pages(page: int, count: int) -> Command:
    if not 0 < count <= 0xFFFF:
        raise ValueError(f"page count out of range: {count}")
    return Command(struct.pack(">BBII", 0xF0, 0x22, page, count), count * PAGE_SIZE, True)


def build_cbw(tag: int, command: Command) -> bytes:
    cdb = command.cdb
    if not 1 <= len(cdb) <= 16:
        raise ValueError(f"CDB length out of range: {len(cdb)}")
    return struct.pack(
        "<4sIIBBB16s",
        CBW_SIGNATURE,
        tag & 0xFFFFFFFF,
        command.length,
        0x80 if command.direction_in else 0,
        0,
        len(cdb),
        cdb,
    )


def parse_csw(data: bytes, tag: int) -> tuple[int, int]:
    """Return ``(residue, status)`` of the status wrapper for ``tag``."""
    if len(data) != CSW_LENGTH:
        raise GrainMediaError(f"status wrapper is {len(data)} bytes, want {CSW_LENGTH}")
    signature, got, residue, status = struct.unpack("<4sIIB", data)
    if signature != CSW_SIGNATURE:
        raise GrainMediaError(f"bad status signature {signature!r}")
    if got != tag & 0xFFFFFFFF:
        raise GrainMediaError(f"status tag {got:#010x} does not match command {tag:#010x}")
    return residue, status


def swap_words(data: bytes) -> bytes:
    """Reverse the bytes of every 32-bit word.

    The SPI controller moves data as 32-bit frames, most significant byte
    first, so what the device calls a word lands in flash byte-swapped. Images
    and dumps in defib are always in flash order, as a programmer clip sees
    them; this converts at the USB edge, in both directions.
    """
    if len(data) % 4:
        raise ValueError(f"{len(data)} bytes is not a whole number of words")
    out = bytearray(len(data))
    for i in range(4):
        out[i::4] = data[3 - i::4]
    return bytes(out)


def has_system_header(image: bytes) -> bool:
    """Does this flash-order image start with a GM8126 system header?"""
    if len(image) < PAGE_SIZE:
        return False
    header = swap_words(image[:PAGE_SIZE])
    return header.startswith(HEADER_MAGIC) and header.endswith(HEADER_END)


def validate_boot_image(image: bytes) -> None:
    """Check what the SoC needs to find at the start of flash.

    Raises:
        GrainMediaError: naming what is wrong. A byte-swapped image (the order
            the vendor tool's own files are in) gets its own message.
    """
    if not has_system_header(image):
        if image[:PAGE_SIZE].startswith(HEADER_MAGIC):
            raise GrainMediaError(
                "the image is in the vendor tool's byte order, not in flash order: "
                "reverse the bytes of each 32-bit word of the header and nsboot"
            )
        raise GrainMediaError("the image has no GM8126 system header at offset 0")
    header = swap_words(image[:PAGE_SIZE])
    page_log2, sector_log2 = struct.unpack_from("<II", header, 0x34)
    if page_log2 != PAGE_LOG2:
        raise GrainMediaError(f"the system header names {page_log2}-bit pages, want {PAGE_LOG2}")
    if not PAGE_LOG2 < sector_log2 <= ERASE_LOG2:
        raise GrainMediaError(f"the system header names {sector_log2}-bit sectors")
    start = 1 << sector_log2
    if len(image) < start + PAGE_SIZE + NSBOOT_LOAD_SIZE:
        raise GrainMediaError("the image ends inside its first-stage loader")
    (magic,) = struct.unpack("<I", swap_words(image[start:start + 4]))
    if magic != IMAGE_MAGIC:
        raise GrainMediaError(f"no nsboot image header at {start:#x} (found {magic:#010x})")


def check_inquiry(data: bytes) -> str:
    """Return the product string, refusing anything that is not a GM8126."""
    if len(data) < INQUIRY_LENGTH or not data.startswith(INQUIRY_PREFIX):
        raise GrainMediaError("the device does not identify itself as a Grain Media updater")
    product = data[16:32].decode("ascii", "replace").strip("\0 ")
    if product != "GM8126":
        raise GrainMediaError(f"the device says it is {product!r}, not a GM8126")
    return product
