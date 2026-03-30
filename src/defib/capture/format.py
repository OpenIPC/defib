"""Binary .dcap capture format for recording and replaying UART sessions.

Format:
    Header (48 bytes):
        Magic:     4B  "DCAP"
        Version:   2B  uint16 LE (0x0001)
        Baudrate:  4B  uint32 LE
        Chip:      32B null-padded UTF-8
        Flags:     2B  uint16 LE (bit 0: has timing)
        Reserved:  4B  zeros

    Records (variable, repeated):
        Timestamp: 8B  uint64 LE (microseconds since capture start)
        Direction: 1B  (0x00=TX host→device, 0x01=RX device→host)
        Length:    2B  uint16 LE
        Data:      [Length] bytes
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


DCAP_MAGIC = b"DCAP"
DCAP_VERSION = 1
HEADER_SIZE = 48
HEADER_FORMAT = "<4sHI32sHI"  # magic, version, baudrate, chip, flags, reserved

FLAG_HAS_TIMING = 0x01


class Direction(IntEnum):
    TX = 0  # Host → Device
    RX = 1  # Device → Host


@dataclass
class CaptureRecord:
    """A single captured I/O operation."""
    timestamp_us: int
    direction: Direction
    data: bytes


@dataclass
class CaptureFile:
    """A complete .dcap capture file."""
    baudrate: int = 115200
    chip: str = ""
    flags: int = FLAG_HAS_TIMING
    records: list[CaptureRecord] = field(default_factory=list)

    def add_tx(self, timestamp_us: int, data: bytes) -> None:
        """Record a host → device transmission."""
        self.records.append(CaptureRecord(timestamp_us, Direction.TX, data))

    def add_rx(self, timestamp_us: int, data: bytes) -> None:
        """Record a device → host reception."""
        self.records.append(CaptureRecord(timestamp_us, Direction.RX, data))

    @property
    def duration_us(self) -> int:
        """Total capture duration in microseconds."""
        if not self.records:
            return 0
        return self.records[-1].timestamp_us - self.records[0].timestamp_us

    @property
    def tx_bytes(self) -> int:
        """Total bytes transmitted (host → device)."""
        return sum(len(r.data) for r in self.records if r.direction == Direction.TX)

    @property
    def rx_bytes(self) -> int:
        """Total bytes received (device → host)."""
        return sum(len(r.data) for r in self.records if r.direction == Direction.RX)

    def encode(self) -> bytes:
        """Serialize to .dcap binary format."""
        # Header
        chip_bytes = self.chip.encode("utf-8")[:32].ljust(32, b"\x00")
        header = struct.pack(
            HEADER_FORMAT,
            DCAP_MAGIC,
            DCAP_VERSION,
            self.baudrate,
            chip_bytes,
            self.flags,
            0,  # reserved
        )
        assert len(header) == HEADER_SIZE

        # Records
        parts = [header]
        for record in self.records:
            rec_header = struct.pack(
                "<QBH",
                record.timestamp_us,
                record.direction,
                len(record.data),
            )
            parts.append(rec_header)
            parts.append(record.data)

        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> CaptureFile:
        """Deserialize from .dcap binary format."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"File too small: {len(data)} bytes (need {HEADER_SIZE})")

        magic, version, baudrate, chip_bytes, flags, _ = struct.unpack_from(
            HEADER_FORMAT, data, 0
        )

        if magic != DCAP_MAGIC:
            raise ValueError(f"Invalid magic: {magic!r} (expected {DCAP_MAGIC!r})")
        if version != DCAP_VERSION:
            raise ValueError(f"Unsupported version: {version}")

        chip = chip_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")

        capture = cls(baudrate=baudrate, chip=chip, flags=flags)

        offset = HEADER_SIZE
        while offset < len(data):
            if offset + 11 > len(data):
                break  # Incomplete record header

            timestamp_us, direction, length = struct.unpack_from("<QBH", data, offset)
            offset += 11

            if offset + length > len(data):
                break  # Incomplete record data

            rec_data = data[offset:offset + length]
            offset += length

            capture.records.append(CaptureRecord(
                timestamp_us=timestamp_us,
                direction=Direction(direction),
                data=rec_data,
            ))

        return capture

    def save(self, path: str | Path) -> None:
        """Write capture to a file."""
        Path(path).write_bytes(self.encode())

    @classmethod
    def load(cls, path: str | Path) -> CaptureFile:
        """Load capture from a file."""
        return cls.decode(Path(path).read_bytes())
