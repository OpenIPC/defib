"""Frame builders and parsers for HiSilicon boot protocol.

Frame types:
- HEAD: Initiates a data transfer with length and target address
- DATA: Carries payload chunks with sequence numbers
- TAIL: Marks the end of a data transfer

All frames use big-endian byte order and CRC-16/CCITT checksums.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from defib.protocol.crc import MAX_DATA_LEN, calc_crc

# Frame magic bytes
HEAD_MAGIC = bytes([0xFE, 0x00, 0xFF, 0x01])
DATA_MAGIC = 0xDA
TAIL_MAGIC = 0xED

# V500 handshake magic
V500_HANDSHAKE_MAGIC = bytes([0xBD, 0x00, 0xFF, 0x01])

# CV6xx handshake magic
CV6XX_HANDSHAKE_MAGIC = bytes([0xEF, 0xBE, 0xAD, 0xDE, 0x12, 0x00, 0xF0, 0x0F])

# CV6xx board ID query magic
CV6XX_BOARDID_MAGIC = bytes([0xCE, 0x00, 0xFF, 0x01])

# Boot file magic numbers for CV6xx
CV6XX_GSL_MAGIC = 0x4BB4D22D
CV6XX_DDR_PARAMS_MAGIC = 0x4B87A52D
CV6XX_UBOOT_MAGIC = 0x4BF01E2D


class FrameError(Exception):
    """Invalid frame data."""


@dataclass
class HeadFrame:
    """HEAD frame: initiates a data transfer."""
    length: int
    address: int

    def encode(self) -> bytes:
        frame = bytearray(14)
        frame[0:4] = HEAD_MAGIC
        struct.pack_into(">I", frame, 4, self.length)
        struct.pack_into(">I", frame, 8, self.address)
        crc = calc_crc(frame[:12])
        frame[12] = (crc >> 8) & 0xFF
        frame[13] = crc & 0xFF
        return bytes(frame)

    @classmethod
    def decode(cls, data: bytes) -> HeadFrame:
        if len(data) < 14:
            raise FrameError(f"HEAD frame too short: {len(data)} bytes")
        if data[0:4] != HEAD_MAGIC:
            raise FrameError(f"Invalid HEAD magic: {data[0:4].hex()}")
        length = struct.unpack_from(">I", data, 4)[0]
        address = struct.unpack_from(">I", data, 8)[0]
        return cls(length=length, address=address)


@dataclass
class DataFrame:
    """DATA frame: carries a payload chunk."""
    seq: int
    payload: bytes

    def encode(self) -> bytes:
        head = bytearray(3)
        head[0] = DATA_MAGIC
        head[1] = self.seq & 0xFF
        head[2] = (~self.seq) & 0xFF
        data = bytes(head) + self.payload
        crc = calc_crc(data)
        return data + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

    @classmethod
    def decode(cls, data: bytes) -> DataFrame:
        if len(data) < 6:
            raise FrameError(f"DATA frame too short: {len(data)} bytes")
        if data[0] != DATA_MAGIC:
            raise FrameError(f"Invalid DATA magic: 0x{data[0]:02x}")
        seq = data[1]
        expected_inv = (~seq) & 0xFF
        if data[2] != expected_inv:
            raise FrameError(f"Sequence inversion mismatch: {data[2]} != {expected_inv}")
        payload = data[3:-2]
        return cls(seq=seq, payload=payload)


@dataclass
class TailFrame:
    """TAIL frame: marks end of a data transfer."""
    seq: int

    def encode(self) -> bytes:
        data = bytearray(3)
        data[0] = TAIL_MAGIC
        data[1] = self.seq & 0xFF
        data[2] = (~self.seq) & 0xFF
        crc = calc_crc(data)
        return bytes(data) + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

    @classmethod
    def decode(cls, data: bytes) -> TailFrame:
        if len(data) < 5:
            raise FrameError(f"TAIL frame too short: {len(data)} bytes")
        if data[0] != TAIL_MAGIC:
            raise FrameError(f"Invalid TAIL magic: 0x{data[0]:02x}")
        seq = data[1]
        return cls(seq=seq)


def chunk_data(data: bytes, chunk_size: int = MAX_DATA_LEN) -> list[bytes]:
    """Split data into chunks of at most chunk_size bytes."""
    return [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
