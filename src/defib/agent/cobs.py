"""COBS (Consistent Overhead Byte Stuffing) encoding/decoding.

COBS replaces all zero bytes in a packet so that 0x00 can be used
as an unambiguous frame delimiter. Overhead: at most ceil(n/254)
bytes for an n-byte message (~0.4%).

Frame format on wire: [COBS-encoded data] [0x00]
"""

from __future__ import annotations


def encode(data: bytes) -> bytes:
    """COBS-encode data. The result contains no zero bytes.

    Standard COBS: each overhead byte indicates the distance to the
    next zero (or end of block). 0xFF means 254 non-zero bytes follow
    with no implicit zero after.
    """
    output = bytearray()
    code_idx = len(output)
    output.append(0)  # Placeholder for first code byte
    code = 1

    for byte in data:
        if byte == 0x00:
            output[code_idx] = code
            code_idx = len(output)
            output.append(0)  # Placeholder for next code
            code = 1
        else:
            output.append(byte)
            code += 1
            if code == 0xFF:
                output[code_idx] = code
                code_idx = len(output)
                output.append(0)  # Placeholder
                code = 1

    output[code_idx] = code
    return bytes(output)


def decode(data: bytes) -> bytes:
    """COBS-decode data (without the trailing 0x00 delimiter)."""
    output = bytearray()
    idx = 0
    while idx < len(data):
        code = data[idx]
        idx += 1
        if code == 0:
            raise ValueError("Unexpected zero in COBS-encoded data")
        for _ in range(code - 1):
            if idx >= len(data):
                raise ValueError("COBS decode: truncated data")
            output.append(data[idx])
            idx += 1
        if code < 0xFF and idx < len(data):
            output.append(0x00)

    return bytes(output)
