"""HiSilicon CV6xx boot recovery protocol for HI3516CV6xx series.

Protocol flow:
1. Handshake: Send DEADBEEF magic with baud rate, loop until "uart ddr"/"uart flash"
2. Board ID query: Send CE frame with timestamps, get CPU/Board ID
3. Parse composite boot file: GSL + DDR params (multiple tables) + U-Boot
4. Transfer: GSL → 0x04021A00, DDR table → 0x41000000, wait DDR training, U-Boot → 0x41000000
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Callable

from defib.protocol.base import BootProtocol, ProtocolError
from defib.protocol.crc import ACK_BYTE, append_crc, append_crc_le
from defib.protocol.frames import (
    CV6XX_BOARDID_MAGIC,
    CV6XX_DDR_PARAMS_MAGIC,
    CV6XX_GSL_MAGIC,
    CV6XX_HANDSHAKE_MAGIC,
    CV6XX_UBOOT_MAGIC,
)
from defib.protocol.registry import register
from defib.recovery.events import (
    HandshakeResult,
    ProgressEvent,
    RecoveryResult,
    Stage,
)
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

CV6XX_SOCS = frozenset([
    "hi3516cv608", "hi3516cv610", "hi3516cv613",
    "hi3516dv500", "hi3519dv500",
])

# The CV6xx GSL protocol downloads the composite image at CP_STEP1_ADDR,
# after the BootROM/GSL stack and BSS reserved at the start of SRAM.
GSL_LOAD_ADDR = 0x04021A00
DDR_LOAD_ADDR = 0x41000000
UBOOT_LOAD_ADDR = 0x41000000
DDR_TRAINING_WAIT = 1.5  # seconds

IMAGE_ALIGNMENT = 0x100
CODE_ALIGNMENT = 0x200
IMAGE_HEADER_FIELDS_SIZE = 40
DDR_PARAMS_FIELDS_SIZE = 308
MAX_GSL_HEADER_OFFSET = 0x10000
MAX_SECTION_GAP = 0x1000
MAX_DDR_TABLES = 8


def _emit(callback: Callable[[ProgressEvent], None] | None, event: ProgressEvent) -> None:
    if callback is not None:
        callback(event)


@dataclass
class CV6xxBootParts:
    """Parsed sections of a CV6xx composite boot file."""
    gsl_data: bytes
    gsl_size: int
    params_start: int
    offset_32: int
    table_count: int
    table_size: int
    board_mapping: bytes
    uboot_data: bytes
    file_data: bytes


@dataclass(frozen=True)
class _CodeImage:
    """Validated code-image header and payload bounds."""

    offset: int
    structure_length: int
    payload_length: int

    @property
    def end(self) -> int:
        return self.offset + self.structure_length + self.payload_length


@dataclass(frozen=True)
class _DdrParamsImage:
    """Validated DDR parameter-image header and table bounds."""

    offset: int
    structure_length: int
    table_offset: int
    table_size: int
    table_count: int
    board_mapping: bytes

    @property
    def tables_end(self) -> int:
        return self.table_offset + self.table_size * self.table_count


def _magic_offsets(
    file_data: bytes,
    magic: int,
    start: int = 0,
    end: int | None = None,
) -> Iterator[int]:
    """Yield aligned occurrences of an image magic within the requested range."""
    magic_bytes = struct.pack("<I", magic)
    limit = len(file_data) if end is None else min(end, len(file_data))
    offset = file_data.find(magic_bytes, start, limit)
    while offset >= 0:
        if offset % IMAGE_ALIGNMENT == 0:
            yield offset
        offset = file_data.find(magic_bytes, offset + 1, limit)


def _parse_code_image(file_data: bytes, offset: int, magic: int, name: str) -> _CodeImage:
    """Validate a GSL/U-Boot image header and return its payload bounds."""
    if offset + IMAGE_HEADER_FIELDS_SIZE > len(file_data):
        raise ProtocolError(f"Truncated {name} header at 0x{offset:X}")

    image_magic, version, structure_length, signature_length = struct.unpack_from(
        "<4I", file_data, offset,
    )
    payload_length = struct.unpack_from("<I", file_data, offset + 36)[0]

    if image_magic != magic:
        raise ProtocolError(f"Invalid {name} magic at 0x{offset:X}")
    if version == 0:
        raise ProtocolError(f"Invalid {name} structure version at 0x{offset:X}")
    if (
        structure_length < IMAGE_HEADER_FIELDS_SIZE
        or structure_length % IMAGE_ALIGNMENT != 0
        or signature_length == 0
        or signature_length > structure_length
    ):
        raise ProtocolError(f"Invalid {name} structure bounds at 0x{offset:X}")
    if payload_length == 0 or payload_length % CODE_ALIGNMENT != 0:
        raise ProtocolError(f"Invalid {name} payload length at 0x{offset:X}")

    image = _CodeImage(offset, structure_length, payload_length)
    if image.end > len(file_data):
        raise ProtocolError(f"Truncated {name} payload at 0x{offset:X}")
    return image


def _parse_ddr_params(file_data: bytes, offset: int) -> _DdrParamsImage:
    """Validate a DDR parameter header, board map, and all declared tables."""
    if offset + DDR_PARAMS_FIELDS_SIZE > len(file_data):
        raise ProtocolError(f"Truncated DDR params header at 0x{offset:X}")

    magic, version, structure_length, signature_length = struct.unpack_from(
        "<4I", file_data, offset,
    )
    params_area_offset, table_size, table_count = struct.unpack_from(
        "<3I", file_data, offset + 32,
    )
    board_mapping = file_data[offset + 300:offset + 308]

    if magic != CV6XX_DDR_PARAMS_MAGIC:
        raise ProtocolError(f"Invalid DDR params magic at 0x{offset:X}")
    if version == 0:
        raise ProtocolError(f"Invalid DDR params structure version at 0x{offset:X}")
    if (
        structure_length < DDR_PARAMS_FIELDS_SIZE
        or structure_length % IMAGE_ALIGNMENT != 0
        or signature_length == 0
        or signature_length > structure_length
        or params_area_offset % IMAGE_ALIGNMENT != 0
    ):
        raise ProtocolError(f"Invalid DDR params structure bounds at 0x{offset:X}")
    if (
        table_size == 0
        or table_size % IMAGE_ALIGNMENT != 0
        or not 1 <= table_count <= MAX_DDR_TABLES
    ):
        raise ProtocolError(f"Invalid DDR table dimensions at 0x{offset:X}")
    if not any(index < table_count for index in board_mapping):
        raise ProtocolError(f"DDR board mapping has no valid table at 0x{offset:X}")
    if any(index != 0xFF and index >= table_count for index in board_mapping):
        raise ProtocolError(f"DDR board mapping is out of range at 0x{offset:X}")

    table_offset = offset + structure_length + params_area_offset
    params = _DdrParamsImage(
        offset,
        structure_length,
        table_offset,
        table_size,
        table_count,
        board_mapping,
    )
    if params.tables_end > len(file_data):
        raise ProtocolError(f"Truncated DDR tables at 0x{offset:X}")
    return params


def parse_cv6xx_boot(file_data: bytes) -> CV6xxBootParts:
    """Parse a CV6xx composite boot file into its constituent parts.

    Image-tool generations use different key and image-header sizes, placing
    the GSL header at offsets including 0x800 and 0x1200. Locate a candidate
    header, then accept it only when its declared payload is followed by a
    structurally valid DDR parameter image and U-Boot image.
    """
    layouts: list[CV6xxBootParts] = []
    gsl_scan_end = min(len(file_data), MAX_GSL_HEADER_OFFSET + 4)

    for gsl_offset in _magic_offsets(file_data, CV6XX_GSL_MAGIC, end=gsl_scan_end):
        try:
            gsl = _parse_code_image(file_data, gsl_offset, CV6XX_GSL_MAGIC, "GSL")
        except ProtocolError:
            continue

        params_scan_end = min(len(file_data), gsl.end + MAX_SECTION_GAP + 4)
        for params_offset in _magic_offsets(
            file_data,
            CV6XX_DDR_PARAMS_MAGIC,
            start=gsl.end,
            end=params_scan_end,
        ):
            try:
                params = _parse_ddr_params(file_data, params_offset)
                uboot = _parse_code_image(
                    file_data,
                    params.tables_end,
                    CV6XX_UBOOT_MAGIC,
                    "U-Boot",
                )
            except ProtocolError:
                continue

            layouts.append(CV6xxBootParts(
                gsl_data=file_data[:gsl.end],
                gsl_size=gsl.end,
                params_start=params.offset,
                offset_32=params.table_offset - params.offset - params.structure_length,
                table_count=params.table_count,
                table_size=params.table_size,
                board_mapping=params.board_mapping,
                uboot_data=file_data[uboot.offset:uboot.end],
                file_data=file_data,
            ))

    if not layouts:
        raise ProtocolError("No structurally valid CV6xx GSL/DDR/U-Boot layout found")
    if len(layouts) > 1:
        raise ProtocolError("Ambiguous CV6xx boot image: multiple valid GSL layouts found")
    return layouts[0]


def wrap_cv6xx_payload(header_source: bytes, payload: bytes) -> bytes:
    """Wrap an arbitrary blob in the CV6xx U-Boot header.

    The CV6xx bootrom validates the U-Boot magic at byte 0 and reads
    the code-offset field at byte 8 (= 0x400) to decide where to jump
    inside the loaded blob. ``header_source`` is the uboot_data section
    of a valid composite boot file (e.g. ``parse_cv6xx_boot(blob).uboot_data``);
    we reuse its 1024-byte header verbatim and patch only the payload
    length field at byte 36. Callers must link the payload at
    ``UBOOT_LOAD_ADDR + 0x400`` so its first instruction lands where
    the bootrom jumps.
    """
    if len(header_source) < 1024:
        raise ValueError("header_source must be at least 1024 bytes")
    header = bytearray(header_source[:1024])
    struct.pack_into("<I", header, 36, len(payload))
    return bytes(header) + payload


def build_ddr_table(parts: CV6xxBootParts, board_id: int = 0) -> bytes:
    """Build the DDR initialization table for a specific board ID."""
    mapping = parts.board_mapping
    mapped_index = board_id if board_id < len(mapping) else 0
    mapped_index = mapping[mapped_index]

    if mapped_index >= parts.table_count:
        mapped_index = 0

    params_structure_length = struct.unpack_from(
        "<I", parts.file_data, parts.params_start + 8,
    )[0]
    first_table_offset = (
        parts.params_start + params_structure_length + parts.offset_32
    )

    ddr_buf = bytearray()
    # Preserve the REE key and DDR image headers, then append the selected table.
    ddr_buf.extend(parts.file_data[parts.gsl_size:first_table_offset])
    table_offset = first_table_offset + (mapped_index * parts.table_size)
    ddr_buf.extend(parts.file_data[table_offset:table_offset + parts.table_size])

    return bytes(ddr_buf)


@register
class HiSiliconCV6xx(BootProtocol):
    """HI3516CV6xx series boot protocol."""

    def __init__(self, ddr_training_wait: float = DDR_TRAINING_WAIT) -> None:
        self._board_id: int = 0
        self._cpu_id: int | None = None
        self._ddr_training_wait = ddr_training_wait

    @classmethod
    def name(cls) -> str:
        return "HiSilicon CV6xx"

    @classmethod
    def matches(cls, chip_name: str) -> bool:
        return chip_name.lower() in CV6XX_SOCS

    async def handshake(
        self,
        transport: Transport,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> HandshakeResult:
        """Send DEADBEEF handshake until 'uart ddr' or 'uart flash' response."""
        _emit(on_progress, ProgressEvent(
            stage=Stage.HANDSHAKE, bytes_sent=0, bytes_total=1,
            message="Waiting for bootrom... power-cycle the device now!",
        ))

        # Build handshake frame: magic + baudrate(LE) + serial params + CRC(LE)
        frame = bytearray(CV6XX_HANDSHAKE_MAGIC)
        frame += struct.pack("<I", 115200)  # baudrate
        frame += bytearray([8, 1, 0, 9])   # serial format params
        frame_bytes = append_crc_le(frame)

        buffer = bytearray()
        while True:
            await transport.write(frame_bytes)
            await asyncio.sleep(0.01)

            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    chunk = await transport.read(waiting, timeout=0.01)
                    buffer += chunk
                    if b"uart ddr" in buffer or b"uart flash" in buffer:
                        await asyncio.sleep(0.5)
                        await transport.flush_input()
                        _emit(on_progress, ProgressEvent(
                            stage=Stage.HANDSHAKE, bytes_sent=1, bytes_total=1,
                            message="BootROM handshake complete",
                        ))
                        return HandshakeResult(
                            success=True,
                            message="BootROM handshake complete",
                        )
            except TransportTimeout:
                continue

    async def _get_board_id(self, transport: Transport) -> int:
        """Query the device for board ID."""
        t_bytes = struct.pack(">I", int(time.time()))
        frame = bytearray(CV6XX_BOARDID_MAGIC) + t_bytes + t_bytes
        frame_bytes = append_crc(frame)

        await transport.flush_input()
        await transport.write(frame_bytes)

        buf = bytearray()
        start = time.monotonic()
        while time.monotonic() - start < 2.0:
            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    chunk = await transport.read(waiting, timeout=0.1)
                    buf += chunk
                    if b"\xce" in buf:
                        idx = buf.index(b"\xce")
                        if len(buf) >= idx + 11 and buf[idx + 10] == 0xAA:
                            resp = buf[idx:idx + 11]
                            self._cpu_id = resp[1]
                            board_id = struct.unpack(">I", resp[4:8])[0]
                            self._board_id = board_id
                            # Push back any unconsumed bytes after the response
                            remaining = buf[idx + 11:]
                            if remaining:
                                try:
                                    await transport.unread(bytes(remaining))
                                except NotImplementedError:
                                    pass
                            return int(board_id)
            except TransportTimeout:
                pass
            await asyncio.sleep(0.01)

        return 0

    async def _send_frame_wait_ack(
        self,
        transport: Transport,
        data: bytes,
        timeout: float = 4.0,
    ) -> bool:
        """Send frame and wait for ACK with NAK retransmission."""
        await transport.write(data)
        retries = 0
        start = time.monotonic()

        while time.monotonic() - start < timeout and retries < 10:
            try:
                response = await transport.read(1, timeout=timeout)
            except TransportTimeout:
                return False

            if response == ACK_BYTE:
                return True
            if response == b"U":
                await transport.write(data)
                retries += 1
                continue

        return False

    async def _send_data_to_bootrom(
        self,
        transport: Transport,
        data: bytes,
        address: int,
        stage: Stage,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> bool:
        """Send data using V500-style HEAD/DATA/TAIL with per-chunk ACK."""
        total = len(data)

        # HEAD frame
        head = b"\xfe\x00\xff\x01"
        head += struct.pack(">I", total)
        head += struct.pack(">I", address)
        head = append_crc(head)

        if not await self._send_frame_wait_ack(transport, head):
            return False

        # DATA frames
        idx = 0
        pos = 0
        remaining = total
        while remaining > 0:
            idx += 1
            chunk_size = min(1024, remaining)
            chunk = data[pos:pos + chunk_size]
            pos += chunk_size
            remaining -= chunk_size

            frame = b"\xda"
            frame += struct.pack("B", idx & 0xFF)
            frame += struct.pack("B", (~idx) & 0xFF)
            frame += chunk
            frame = append_crc(frame)

            _emit(on_progress, ProgressEvent(
                stage=stage, bytes_sent=pos, bytes_total=total,
            ))

            if not await self._send_frame_wait_ack(transport, frame):
                return False

        # TAIL frame
        count = ((total + 1023) // 1024) + 1
        tail = b"\xed"
        tail += struct.pack("B", count & 0xFF)
        tail += struct.pack("B", (~count) & 0xFF)
        tail = append_crc(tail)

        if not await self._send_frame_wait_ack(transport, tail):
            return False

        _emit(on_progress, ProgressEvent(
            stage=stage, bytes_sent=total, bytes_total=total,
        ))
        return True

    async def send_firmware(
        self,
        transport: Transport,
        firmware: bytes,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        uboot_override: bytes | None = None,
    ) -> RecoveryResult:
        stages: list[Stage] = []

        # Parse the composite boot file
        try:
            parts = parse_cv6xx_boot(firmware)
        except ProtocolError as e:
            return RecoveryResult(success=False, error=str(e))

        uboot_payload = uboot_override if uboot_override is not None else parts.uboot_data

        logger.info(
            "CV6xx boot file: GSL=%d bytes, DDR tables=%d (size=%d), U-Boot=%d bytes",
            len(parts.gsl_data), parts.table_count, parts.table_size, len(uboot_payload),
        )

        # 1. Send GSL
        if not await self._send_data_to_bootrom(
            transport, parts.gsl_data, GSL_LOAD_ADDR,
            Stage.GSL, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send GSL",
            )
        stages.append(Stage.GSL)

        # 2. Get board ID and build DDR table
        _emit(on_progress, ProgressEvent(
            stage=Stage.BOARD_ID, bytes_sent=0, bytes_total=1,
            message="Querying board ID...",
        ))
        board_id = await self._get_board_id(transport)
        logger.info("Board ID: %d, CPU ID: %s", board_id, hex(self._cpu_id or 0))
        _emit(on_progress, ProgressEvent(
            stage=Stage.BOARD_ID, bytes_sent=1, bytes_total=1,
            message=f"Board ID: {board_id}",
        ))

        ddr_table = build_ddr_table(parts, board_id)

        # 3. Send DDR table
        if not await self._send_data_to_bootrom(
            transport, ddr_table, DDR_LOAD_ADDR,
            Stage.DDR_TABLE, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send DDR table",
            )
        stages.append(Stage.DDR_TABLE)

        # 4. Wait for DDR training
        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_TRAINING, bytes_sent=0, bytes_total=1,
            message="Waiting for DDR training...",
        ))
        start = time.monotonic()
        while time.monotonic() - start < self._ddr_training_wait:
            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    # Read in small chunks to avoid consuming data meant for
                    # the next transfer stage (max 256 bytes per iteration)
                    chunk = await transport.read(min(waiting, 256), timeout=0.05)
                    # Log ASCII output from device during DDR training
                    ascii_str = "".join(
                        chr(b) for b in chunk if 32 <= b <= 126 or b in (10, 13)
                    )
                    if ascii_str.strip():
                        logger.info("DDR training: %s", ascii_str.strip())
            except TransportTimeout:
                pass
            await asyncio.sleep(0.05)

        _emit(on_progress, ProgressEvent(
            stage=Stage.DDR_TRAINING, bytes_sent=1, bytes_total=1,
            message="DDR training complete",
        ))
        stages.append(Stage.DDR_TRAINING)

        # 5. Send U-Boot (or the override payload, e.g. a flash agent
        #    wrapped via wrap_cv6xx_payload using the composite's header).
        if not await self._send_data_to_bootrom(
            transport, uboot_payload, UBOOT_LOAD_ADDR,
            Stage.UBOOT, on_progress,
        ):
            return RecoveryResult(
                success=False, stages_completed=stages,
                error="Failed to send U-Boot",
            )
        stages.append(Stage.UBOOT)

        _emit(on_progress, ProgressEvent(
            stage=Stage.COMPLETE, bytes_sent=1, bytes_total=1,
            message="Recovery complete",
        ))
        stages.append(Stage.COMPLETE)
        return RecoveryResult(success=True, stages_completed=stages)
