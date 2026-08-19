"""rockusb bulk stage: CBW/CSW framing and Rockchip opcodes.

Once the usbplug is running the device speaks a dialect of USB Mass Storage
Bulk-Only Transport: a 31-byte command block wrapper out, an optional data
phase, then a 13-byte status wrapper back.  The command descriptor block is
Rockchip's own, and — unlike the wrappers around it — is big-endian.

As with :mod:`defib.rockusb.maskrom`, this module is pure framing so it can be
tested without libusb.
"""

from __future__ import annotations

import struct
from enum import IntEnum

CBW_SIGNATURE = b"USBC"
CSW_SIGNATURE = b"USBS"

CBW_LENGTH = 31
CSW_LENGTH = 13

#: The usbplug presents flash as 512-byte logical blocks regardless of the
#: underlying medium; on SPI NAND it runs Rockchip's FTL underneath, so bad
#: blocks, wear levelling and ECC are all handled device-side.
SECTOR_SIZE = 512

#: Conservative ceiling for one READ_LBA/WRITE_LBA. The count field is 16-bit
#: so the protocol permits far more, but the usbplug's own buffering is
#: undocumented and the reference tools stay at or below this.
MAX_SECTORS_PER_TRANSFER = 512

DIRECTION_IN = 0x80
DIRECTION_OUT = 0x00


class Opcode(IntEnum):
    """Rockchip CDB operation codes (only the ones we actually issue)."""

    TEST_UNIT_READY = 0x00
    READ_FLASH_ID = 0x01
    ERASE_NORMAL = 0x06
    READ_LBA = 0x14
    WRITE_LBA = 0x15
    READ_FLASH_INFO = 0x1A
    READ_CHIP_INFO = 0x1B
    ERASE_LBA = 0x25
    READ_CAPABILITY = 0xAA
    RESET_DEVICE = 0xFF


#: Declared CDB length, which is *not* the 16 bytes the field occupies on the
#: wire. Commands carrying an address declare 10; the rest declare 6. Sending
#: 16 makes the usbplug ignore the wrapper outright — the command never lands
#: and the host waits out its timeout with no error to explain it.
CDB_LENGTH_SHORT = 6
CDB_LENGTH_ADDRESSED = 10

_ADDRESSED_OPCODES = frozenset({
    Opcode.READ_LBA,
    Opcode.WRITE_LBA,
    Opcode.ERASE_LBA,
})


def cdb_length(opcode: Opcode | int) -> int:
    """How long this opcode's command block claims to be."""
    return (
        CDB_LENGTH_ADDRESSED
        if opcode in _ADDRESSED_OPCODES
        else CDB_LENGTH_SHORT
    )


def residue_is_meaningful(opcode: Opcode | int) -> bool:
    """Whether this opcode's status wrapper reports a usable residue.

    Mass Storage says residue is a little-endian count of bytes *not*
    transferred, and on the LBA path an RV1106 usbplug honours that — a
    full-sector read comes back with residue 0.

    Everything else reports nonsense. Measured::

        TEST_UNIT_READY  transfer 0   residue 0x06000000
        READ_FLASH_ID    transfer 5   residue 0x05000000
        READ_CAPABILITY  transfer 8   residue 0x08000000
        READ_FLASH_INFO  transfer 11  residue 0x0B000000
        READ_LBA         transfer 512 residue 0

    Those are the transfer lengths written big-endian, i.e. "none of it
    arrived" — while the data plainly did arrive and is correct. xrock never
    looks at residue at all, which is presumably why nobody noticed.

    So the check is kept exactly where it earns its keep: the block path, the
    one place a short transfer means a partially written flash.
    """
    return opcode in _ADDRESSED_OPCODES


class ResetSubcode(IntEnum):
    """Sub-selector for :attr:`Opcode.RESET_DEVICE`."""

    NORMAL = 0
    RESET_MSC = 1
    POWEROFF = 2
    MASKROM = 3
    DISCONNECT = 4


class CommandStatus(IntEnum):
    OK = 0
    FAILED = 1


class RockusbError(Exception):
    """A rockusb command failed, or the reply did not frame correctly."""


def build_cbw(
    tag: int,
    opcode: Opcode | int,
    *,
    subcode: int = 0,
    address: int = 0,
    count: int = 0,
    transfer_length: int = 0,
    direction_in: bool = False,
) -> bytes:
    """Build the 31-byte command block wrapper.

    Mind the mixed endianness: the wrapper's ``tag`` and ``length`` are
    little-endian, but ``address`` and ``count`` inside the CDB are
    big-endian.

    Args:
        tag: caller-chosen id, echoed back in the status wrapper.
        opcode: Rockchip operation code.
        subcode: CDB byte 1 — the reset selector, or the read/write method.
        address: starting LBA, for the block opcodes.
        count: sector count, for the block opcodes.
        transfer_length: bytes in the data phase. Defaults to
            ``count * SECTOR_SIZE`` when a count is given.
        direction_in: True when the data phase flows device to host.
    """
    if transfer_length == 0 and count:
        transfer_length = count * SECTOR_SIZE

    cdb = struct.pack(
        ">BB I B H 7x",
        int(opcode),
        subcode,
        address,
        0,
        count,
    )
    assert len(cdb) == 16, f"CDB must be 16 bytes, got {len(cdb)}"

    return (
        CBW_SIGNATURE
        + struct.pack("<II", tag & 0xFFFFFFFF, transfer_length)
        + bytes(
            [
                DIRECTION_IN if direction_in else DIRECTION_OUT,
                0x00,  # LUN
                cdb_length(opcode),
            ]
        )
        + cdb
    )


def parse_csw(data: bytes, expected_tag: int | None = None) -> tuple[int, int, int]:
    """Parse the 13-byte command status wrapper.

    Returns:
        ``(tag, residue, status)``.

    Raises:
        RockusbError: on a short read, a bad signature, or a tag that does not
            match ``expected_tag``. A mismatched tag means replies have got out
            of step with commands, which is not something to paper over.
    """
    if len(data) < CSW_LENGTH:
        raise RockusbError(
            f"short status wrapper: got {len(data)} bytes, want {CSW_LENGTH}"
        )
    if data[:4] != CSW_SIGNATURE:
        raise RockusbError(
            f"bad status signature {data[:4]!r}, want {CSW_SIGNATURE!r}"
        )

    tag, residue, status = struct.unpack_from("<IIB", data, 4)
    if expected_tag is not None and tag != (expected_tag & 0xFFFFFFFF):
        raise RockusbError(
            f"status tag {tag:#010x} does not match command tag "
            f"{expected_tag & 0xFFFFFFFF:#010x}"
        )
    return tag, residue, status


def split_lba_transfers(
    start_lba: int,
    total_sectors: int,
    max_sectors: int = MAX_SECTORS_PER_TRANSFER,
) -> list[tuple[int, int]]:
    """Split a block range into ``(lba, count)`` pairs of at most ``max_sectors``."""
    if total_sectors < 0:
        raise ValueError(f"negative sector count: {total_sectors}")
    if max_sectors <= 0:
        raise ValueError(f"max_sectors must be positive, got {max_sectors}")

    out: list[tuple[int, int]] = []
    lba = start_lba
    remaining = total_sectors
    while remaining > 0:
        count = min(remaining, max_sectors)
        out.append((lba, count))
        lba += count
        remaining -= count
    return out
