"""Flash operations on a GM8126 in its USB updater.

Addresses and images here are in flash order throughout.

Write order matters more than usual: the SoC comes back to the updater by
itself only while page 0 is not a valid header. So when block 0 is part of a
write it is erased first, and its header page is programmed last, after
everything else has been read back. A run cut short anywhere leaves a camera
that answers on USB again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from defib.grainmedia import protocol as p
from defib.grainmedia.protocol import Command, GrainMediaError
from defib.recovery.events import ProgressEvent, Stage

#: 32 KiB per read.
READ_PAGES = 128
READ_SIZE = READ_PAGES * p.PAGE_SIZE

SIZES = (0x100000, 0x200000, 0x400000, 0x800000)

ProgressCallback = Callable[[ProgressEvent], None]


class CommandDevice(Protocol):
    def command(self, command: Command, data_out: bytes = b"") -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Segment:
    """What a range of flash must hold. ``data`` is padded with FF to a block."""

    name: str
    offset: int
    data: bytes

    def __post_init__(self) -> None:
        if self.offset % p.ERASE_SIZE:
            raise ValueError(f"{self.name}: offset {self.offset:#x} is not 64 KiB aligned")
        if not self.data:
            raise ValueError(f"{self.name}: no data")

    @property
    def end(self) -> int:
        blocks = (len(self.data) + p.ERASE_SIZE - 1) // p.ERASE_SIZE
        return self.offset + blocks * p.ERASE_SIZE


def check_segments(segments: Sequence[Segment], flash_size: int) -> list[Segment]:
    """Sort segments, refusing overlaps and anything past the chip."""
    ordered = sorted(segments, key=lambda s: s.offset)
    for before, after in zip(ordered, ordered[1:]):
        if before.end > after.offset:
            raise GrainMediaError(f"{before.name} overlaps {after.name}")
    for segment in ordered:
        if segment.end > flash_size:
            raise GrainMediaError(
                f"{segment.name} ends at {segment.end:#x}, past the {flash_size:#x}-byte flash"
            )
    return ordered


class GrainMediaRecovery:
    def __init__(self, device: CommandDevice) -> None:
        self._device = device

    def close(self) -> None:
        self._device.close()

    async def _command(self, command: Command, data: bytes = b"") -> bytes:
        return await asyncio.to_thread(self._device.command, command, data)

    async def identify(self) -> str:
        """Check it is a GM8126 on SPI NOR. Returns the product string."""
        product = p.check_inquiry(await self._command(p.inquiry()))
        flash = await self._command(p.read_flash_id())
        if flash != p.FLASH_ID_SPI:
            raise GrainMediaError(
                f"the device reports flash id {flash.hex()}, not SPI NOR: "
                "NAND boards are not supported"
            )
        return product

    async def set_geometry(self, flash_size: int = p.MAX_FLASH_SIZE) -> None:
        await self._command(p.set_spi_geometry(flash_size.bit_length() - 1))

    async def read(
        self,
        offset: int,
        length: int,
        on_chunk: Callable[[bytes], object] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> bytes:
        """Read flash. With ``on_chunk`` the data goes there, not to the result."""
        if offset % p.PAGE_SIZE or length % p.PAGE_SIZE:
            raise ValueError("reads are whole pages")
        if offset < 0 or length < 0 or offset + length > p.MAX_FLASH_SIZE:
            raise ValueError("read outside the flash")
        out = bytearray()
        done = 0
        while done < length:
            size = min(READ_SIZE, length - done)
            raw = await self._command(
                p.read_pages((offset + done) // p.PAGE_SIZE, size // p.PAGE_SIZE)
            )
            chunk = p.swap_words(raw)
            if on_chunk is not None:
                on_chunk(chunk)
            else:
                out += chunk
            done += size
            if on_progress is not None:
                on_progress(ProgressEvent(Stage.FLASH_READ, done, length))
        return bytes(out)

    async def probe_read(self) -> None:
        """Read block 0 twice: the same bytes must come back.

        Raises:
            GrainMediaError: when reading does not work. Without it nothing
                can be backed up or verified, so nothing may be written.
        """
        try:
            first = await self.read(0, p.ERASE_SIZE)
            second = await self.read(0, p.ERASE_SIZE)
        except GrainMediaError as e:
            raise GrainMediaError(
                f"the device did not answer the flash read ({e}); "
                "nothing has been written, and defib does not write what it cannot read back"
            ) from e
        if first != second:
            raise GrainMediaError("two reads of block 0 differ: not a link to write flash over")

    async def detect_size(self) -> int | None:
        """Chip size in bytes, or None when the contents cannot tell.

        The updater has no command that returns the JEDEC id. A chip smaller
        than 16 MiB ignores the high address bits, so its start shows up again
        at its size. A start without variety (an erased chip) proves nothing,
        and neither does a match of it.
        """
        start = await self.read(0, p.ERASE_SIZE)
        if len(set(start)) < 16:
            return None
        for size in SIZES:
            if await self.read(size, p.ERASE_SIZE) == start:
                return size
        return p.MAX_FLASH_SIZE

    async def dump(
        self,
        offset: int,
        length: int,
        write: Callable[[bytes], object],
        on_progress: ProgressCallback | None = None,
    ) -> int:
        await self.read(offset, length, write, on_progress)
        return length

    async def _program(self, offset: int, data: bytes, skip_page0: bool) -> None:
        for at in range(0, len(data), p.PAGE_SIZE):
            page = data[at:at + p.PAGE_SIZE].ljust(p.PAGE_SIZE, b"\xff")
            if page == b"\xff" * p.PAGE_SIZE or (skip_page0 and offset + at == 0):
                continue
            await self._command(
                p.program_page((offset + at) // p.PAGE_SIZE), p.swap_words(page)
            )

    async def _verify(self, offset: int, data: bytes, what: str) -> None:
        got = await self.read(offset, len(data))
        if got != data:
            bad = next(i for i in range(len(data)) if got[i] != data[i])
            raise GrainMediaError(
                f"{what}: flash differs at {offset + bad:#x} "
                f"(read {got[bad]:02x}, want {data[bad]:02x})"
            )

    async def write(
        self,
        segments: Sequence[Segment],
        flash_size: int,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Erase, program and verify. Page 0 goes last; see the module text.

        Returns the bytes of flash rewritten.
        """
        ordered = check_segments(segments, flash_size)
        blocks = [
            (s.name, s.offset + at, s.data[at:at + p.ERASE_SIZE].ljust(p.ERASE_SIZE, b"\xff"))
            for s in ordered
            for at in range(0, s.end - s.offset, p.ERASE_SIZE)
        ]
        header = next((data[:p.PAGE_SIZE] for _, at, data in blocks if at == 0), None)
        await self.probe_read()
        await self.set_geometry(flash_size)

        if header is not None:
            await self._command(p.erase_block(0))
        total = len(blocks) * p.ERASE_SIZE
        done = 0
        blank = b"\xff" * p.PAGE_SIZE
        for _, at, data in blocks:
            if at:
                await self._command(p.erase_block(at))
            await self._program(at, data, skip_page0=True)
            done += p.ERASE_SIZE
            if on_progress is not None:
                on_progress(ProgressEvent(Stage.FLASH_WRITE, done, total))
        # Read back only once everything is written: on a chip smaller than
        # flash_size a later block lands on an earlier one. Page 0 must still
        # be erased, which is also what proves block 0 was.
        for name, at, data in blocks:
            await self._verify(at, blank + data[p.PAGE_SIZE:] if at == 0 else data, name)
        if header is not None:
            await self._program(0, header, skip_page0=False)
            await self._verify(0, header, "system header")
        return total
