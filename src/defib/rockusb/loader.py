"""Parse a Rockchip loader (``MiniLoaderAll.bin``) into its two SRAM blobs.

The container is an ``RKBOOT`` archive: a fixed header, then tables of entries
pointing at blobs elsewhere in the same file.  We only care about the 471
(DDR init) and 472 (usbplug) tables.

Not every loader Rockchip publishes has this container.  The standalone
``rv1106_ddr_*.bin`` / ``rv1106_usbplug_*.bin`` files in rkbin are bare images
with no ``RKBOOT`` header at all, which is exactly why ``rkdeveloptool db``
rejects them (rockchip-linux/rkdeveloptool#105).  Those are handled by
:func:`raw_blobs`, which skips the container entirely — the same escape hatch
``xrock maskrom <ddr> <usbplug>`` uses.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

TAG_BOOT = b"BOOT"
TAG_LOADER = b"LDR "

ENTRY_471 = 1
ENTRY_472 = 2
ENTRY_LOADER = 4

# dwDataOffset, dwDataSize, dwDataDelay — the last 12 bytes of every entry.
_ENTRY_TRAILER = 12
_ENTRY_NAME_BYTES = 40  # WCHAR szName[20], UTF-16LE


class LoaderFormatError(Exception):
    """The file is not a loader we know how to read."""


@dataclass(frozen=True)
class LoaderEntry:
    """One blob referenced by the loader's entry table."""

    name: str
    data: bytes
    delay_ms: int


@dataclass(frozen=True)
class LoaderBlobs:
    """What the MaskROM stage needs out of a loader file."""

    ddr: list[LoaderEntry] = field(default_factory=list)
    usbplug: list[LoaderEntry] = field(default_factory=list)
    rc4_disabled: bool = True

    @property
    def use_rc4(self) -> bool:
        """Whether blobs must be RC4'd before upload."""
        return not self.rc4_disabled


def raw_blobs(ddr: bytes, usbplug: bytes, *, use_rc4: bool = False) -> LoaderBlobs:
    """Wrap two bare images as if they had come out of a container.

    For RV1106 this is the normal path, since rkbin ships its DDR and usbplug
    images headerless.

    Raises:
        LoaderFormatError: if either image is empty — uploading nothing would
            look like a board that never came back.
    """
    empty = [n for n, b in (("ddr", ddr), ("usbplug", usbplug)) if not b]
    if empty:
        raise LoaderFormatError(f"{' and '.join(empty)} image is empty")
    return LoaderBlobs(
        ddr=[LoaderEntry(name="ddr", data=ddr, delay_ms=0)],
        usbplug=[LoaderEntry(name="usbplug", data=usbplug, delay_ms=0)],
        rc4_disabled=not use_rc4,
    )


def _parse_entries(
    data: bytes, offset: int, entry_size: int, count: int
) -> list[LoaderEntry]:
    """Read ``count`` entries of ``entry_size`` bytes starting at ``offset``.

    The three fields we need sit at the *end* of each entry, so they are read
    backwards from the entry stride rather than forwards from its start. That
    sidesteps the one genuinely ambiguous field in the struct — ``emType`` is a
    C enum, so whether it occupies 1 byte or 4 depends on how the producing
    toolchain packed it, and getting it wrong would silently shift every
    subsequent offset.
    """
    if entry_size < _ENTRY_TRAILER + _ENTRY_NAME_BYTES:
        raise LoaderFormatError(
            f"entry size {entry_size} too small to hold a name plus offsets"
        )

    entries: list[LoaderEntry] = []
    for i in range(count):
        base = offset + i * entry_size
        if base + entry_size > len(data):
            raise LoaderFormatError(
                f"entry {i} at {base:#x} runs past end of file ({len(data)} bytes)"
            )

        name_at = base + entry_size - _ENTRY_TRAILER - _ENTRY_NAME_BYTES
        name = (
            data[name_at : name_at + _ENTRY_NAME_BYTES]
            .decode("utf-16-le", errors="replace")
            .rstrip("\x00")
        )
        data_offset, data_size, delay_ms = struct.unpack_from(
            "<III", data, base + entry_size - _ENTRY_TRAILER
        )
        if data_offset + data_size > len(data):
            raise LoaderFormatError(
                f"entry {name!r} blob at {data_offset:#x}+{data_size} "
                f"runs past end of file ({len(data)} bytes)"
            )
        entries.append(
            LoaderEntry(
                name=name,
                data=data[data_offset : data_offset + data_size],
                delay_ms=delay_ms,
            )
        )
    return entries


def parse_loader(data: bytes) -> LoaderBlobs:
    """Parse an ``RKBOOT`` container.

    Raises:
        LoaderFormatError: if the magic is wrong or the tables do not fit. A
            headerless rkbin image lands here — use :func:`raw_blobs` for those.
    """
    if len(data) < 64:
        raise LoaderFormatError(f"file too short to be a loader ({len(data)} bytes)")
    if data[:4] not in (TAG_BOOT, TAG_LOADER):
        raise LoaderFormatError(
            f"expected {TAG_BOOT!r} or {TAG_LOADER!r} magic, got {data[:4]!r} — "
            "a bare rkbin image? those have no container, pass them to raw_blobs()"
        )

    # Header layout up to the entry tables:
    #   uiTag[4] usSize[2] dwVersion[4] dwMergeVersion[4]
    #   STRUCT_RKTIME[7]  emSupportChip[4]
    # then the three (count, offset, size) triples.
    table_at = 4 + 2 + 4 + 4 + 7 + 4

    n471, off471, size471 = struct.unpack_from("<BIB", data, table_at)
    n472, off472, size472 = struct.unpack_from("<BIB", data, table_at + 6)
    # Skip the loader table triple; jump to the two trailing flag bytes.
    flags_at = table_at + 6 + 6 + 6
    rc4_flag = data[flags_at + 1]

    # Both stages are mandatory. A container missing either would parse fine
    # and then upload nothing, leaving the caller waiting out a re-enumeration
    # that was never going to happen — a timeout that blames the board for a
    # bad file.
    missing = [
        name
        for name, count in (("DDR init (471)", n471), ("usbplug (472)", n472))
        if count == 0
    ]
    if missing:
        raise LoaderFormatError(
            f"loader declares no {' or '.join(missing)} entries — "
            "it cannot bring a board up"
        )

    return LoaderBlobs(
        ddr=_parse_entries(data, off471, size471, n471),
        usbplug=_parse_entries(data, off472, size472, n472),
        # The field is named for what it disables: 1 means "no RC4".
        rc4_disabled=bool(rc4_flag),
    )
