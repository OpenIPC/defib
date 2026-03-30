"""HiSilicon CV6xx boot recovery protocol for HI3516CV6xx series.

Protocol flow:
1. Handshake: Send DEADBEEF magic with baud rate, loop until "uart ddr"/"uart flash"
2. Board ID query: Send CE frame with timestamps, get CPU/Board ID
3. Parse composite boot file: GSL + DDR params (multiple tables) + U-Boot
4. Transfer: GSL → 0x04020000, DDR table → 0x41000000, wait DDR training, U-Boot → 0x41000000
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
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

GSL_LOAD_ADDR = 0x04020000
DDR_LOAD_ADDR = 0x41000000
UBOOT_LOAD_ADDR = 0x41000000
DDR_TRAINING_WAIT = 1.5  # seconds


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


def parse_cv6xx_boot(file_data: bytes) -> CV6xxBootParts:
    """Parse a CV6xx composite boot file into its constituent parts.

    The file layout:
    - Offset 2048: GSL (magic 0x4BB4D22D), length at offset 2084
    - After GSL + 1024: DDR params (magic 0x4B87A52D)
    - After DDR params: U-Boot (magic 0x4BF01E2D)
    """
    # 1. GSL
    magic_gsl = struct.unpack("<I", file_data[2048:2052])[0]
    if magic_gsl != CV6XX_GSL_MAGIC:
        raise ProtocolError(
            f"Invalid GSL magic: expected 0x{CV6XX_GSL_MAGIC:08X}, "
            f"got 0x{magic_gsl:08X}"
        )
    gsl_len = struct.unpack("<I", file_data[2084:2088])[0]
    gsl_size = gsl_len + 3072
    gsl_data = file_data[:gsl_size]

    # 2. DDR Params
    params_start = gsl_size + 1024
    magic_params = struct.unpack("<I", file_data[params_start:params_start + 4])[0]
    if magic_params != CV6XX_DDR_PARAMS_MAGIC:
        raise ProtocolError(
            f"Invalid DDR params magic: expected 0x{CV6XX_DDR_PARAMS_MAGIC:08X}, "
            f"got 0x{magic_params:08X}"
        )

    offset_32 = struct.unpack("<I", file_data[params_start + 32:params_start + 36])[0]
    table_size = struct.unpack("<I", file_data[params_start + 36:params_start + 40])[0]
    table_count = struct.unpack("<I", file_data[params_start + 40:params_start + 44])[0]
    board_mapping = file_data[params_start + 300:params_start + 308]

    # 3. U-Boot
    uboot_magic_bytes = struct.pack("<I", CV6XX_UBOOT_MAGIC)
    uboot_magic_offset = file_data.find(uboot_magic_bytes, params_start)
    if uboot_magic_offset == -1:
        raise ProtocolError(f"U-Boot magic 0x{CV6XX_UBOOT_MAGIC:08X} not found")

    uboot_len = struct.unpack("<I", file_data[uboot_magic_offset + 36:uboot_magic_offset + 40])[0]
    uboot_size = uboot_len + 1024
    uboot_data = file_data[uboot_magic_offset:uboot_magic_offset + uboot_size]

    return CV6xxBootParts(
        gsl_data=gsl_data,
        gsl_size=gsl_size,
        params_start=params_start,
        offset_32=offset_32,
        table_count=table_count,
        table_size=table_size,
        board_mapping=board_mapping,
        uboot_data=uboot_data,
        file_data=file_data,
    )


def build_ddr_table(parts: CV6xxBootParts, board_id: int = 0) -> bytes:
    """Build the DDR initialization table for a specific board ID."""
    mapping = parts.board_mapping
    mapped_index = board_id if board_id < len(mapping) else 0
    mapped_index = mapping[mapped_index]

    if mapped_index >= parts.table_count:
        mapped_index = 0

    ddr_buf = bytearray()
    # Header portion from after GSL
    ddr_buf.extend(parts.file_data[parts.gsl_size:parts.gsl_size + 2048])
    # Selected DDR table
    table_offset = parts.gsl_size + 2048 + parts.offset_32 + (mapped_index * parts.table_size)
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
            message="Sending CV6xx handshake...",
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
    ) -> RecoveryResult:
        stages: list[Stage] = []

        # Parse the composite boot file
        try:
            parts = parse_cv6xx_boot(firmware)
        except ProtocolError as e:
            return RecoveryResult(success=False, error=str(e))

        logger.info(
            "CV6xx boot file: GSL=%d bytes, DDR tables=%d (size=%d), U-Boot=%d bytes",
            len(parts.gsl_data), parts.table_count, parts.table_size, len(parts.uboot_data),
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

        # 5. Send U-Boot
        if not await self._send_data_to_bootrom(
            transport, parts.uboot_data, UBOOT_LOAD_ADDR,
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
