"""Host-side protocol for communicating with the flash agent.

Packet format:
  [COBS-encoded payload] [0x00]

Payload before COBS encoding:
  [cmd: 1 byte] [data: N bytes] [crc32: 4 bytes LE]

Commands (host → device):
  0x01  INFO     — Request device info
  0x02  READ     — Read flash: addr(4B LE) + size(4B LE)
  0x03  WRITE    — Write flash: addr(4B LE) + data
  0x04  ERASE    — Erase sectors: addr(4B LE) + size(4B LE)
  0x05  CRC32    — CRC32 of flash region: addr(4B LE) + size(4B LE)
  0x06  REBOOT   — Reset device
  0x07  SELFUPDATE — Update agent: addr(4B LE) + size(4B LE) + crc32(4B LE)
  0x08  SET_BAUD   — Change baud rate: baud(4B LE)
  0x09  SCAN       — Scan flash health

Responses (device → host):
  0x81  INFO_RSP — chip_id(4B) + flash_size(4B) + ram_base(4B)
  0x82  DATA     — seq(2B LE) + data(up to 1024B)
  0x83  ACK      — status(1B): 0=OK, 1=CRC error, 2=flash error
  0x84  CRC32_RSP— crc32(4B LE)
  0x85  READY    — Agent is running and ready
  0x86  SCAN_RSP — scan results
"""

from __future__ import annotations

import struct
import zlib

from defib.agent import cobs
from defib.transport.base import Transport, TransportTimeout

# Commands
CMD_INFO = 0x01
CMD_READ = 0x02
CMD_WRITE = 0x03
CMD_ERASE = 0x04
CMD_CRC32 = 0x05
CMD_REBOOT = 0x06
CMD_SELFUPDATE = 0x07
CMD_SET_BAUD = 0x08
CMD_SCAN = 0x09
CMD_FLASH_PROGRAM = 0x0A

# Responses
RSP_INFO = 0x81
RSP_DATA = 0x82
RSP_ACK = 0x83
RSP_CRC32 = 0x84
RSP_READY = 0x85
RSP_SCAN = 0x86

# ACK status
ACK_OK = 0x00
ACK_CRC_ERROR = 0x01

FRAME_DELIMITER = 0x00
MAX_PACKET_SIZE = 1100  # Max COBS-encoded packet size


def build_packet(cmd: int, data: bytes = b"") -> bytes:
    """Build a COBS-framed packet with CRC32."""
    raw = bytes([cmd]) + data
    crc = struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF)
    encoded = cobs.encode(raw + crc)
    return encoded + b"\x00"


def parse_packet(raw: bytes) -> tuple[int, bytes]:
    """Parse a COBS-encoded packet. Returns (cmd, data)."""
    decoded = cobs.decode(raw)
    if len(decoded) < 5:
        raise ValueError("Packet too short")
    payload = decoded[:-4]
    expected_crc = struct.unpack("<I", decoded[-4:])[0]
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError(f"CRC mismatch: {actual_crc:#x} != {expected_crc:#x}")
    return payload[0], payload[1:]


async def send_packet(transport: Transport, cmd: int, data: bytes = b"") -> None:
    """Send a COBS-framed packet to the device."""
    packet = build_packet(cmd, data)
    port = getattr(transport, '_port', None)
    if port is not None:
        port.write(packet)
    else:
        await transport.write(packet)


_port_buffers: dict[int, bytearray] = {}


def _get_port_buf(port: object) -> bytearray:
    key = id(port)
    if key not in _port_buffers:
        _port_buffers[key] = bytearray()
    return _port_buffers[key]


def _recv_packet_sync(port: object, timeout: float) -> tuple[int, bytes]:
    """Synchronous recv using pyserial directly.

    Uses a per-port buffer for bytes read past the current packet's
    delimiter. Only stores complete unprocessed bytes — never partial
    COBS frame data (which caused corruption in previous versions).
    """
    import time
    portbuf = _get_port_buf(port)
    frame = bytearray()
    old_timeout = port.timeout  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout

    try:
        while time.monotonic() < deadline:
            # Get next chunk of data: from portbuf first, then from port
            if portbuf:
                data = bytes(portbuf)
                portbuf.clear()
            else:
                waiting = port.in_waiting  # type: ignore[attr-defined]
                if waiting > 0:
                    data = port.read(waiting)  # type: ignore[attr-defined]
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    port.timeout = min(remaining, 0.1)  # type: ignore[attr-defined]
                    data = port.read(1)  # type: ignore[attr-defined]

            if not data:
                continue

            for i, byte in enumerate(data):
                if byte == 0x00:
                    if frame:
                        try:
                            result = parse_packet(bytes(frame))
                            # Stash ONLY bytes after this delimiter
                            # (complete unprocessed data for next call)
                            remaining_bytes = data[i + 1:]
                            if remaining_bytes:
                                portbuf.extend(remaining_bytes)
                            frame.clear()
                            return result
                        except ValueError:
                            pass
                        frame.clear()
                else:
                    frame.append(byte)
                    if len(frame) > MAX_PACKET_SIZE:
                        frame.clear()
    finally:
        # Do NOT stash partial frame data — that causes corruption
        # when the next call concatenates it with new bytes.
        port.timeout = old_timeout  # type: ignore[attr-defined]

    raise TransportTimeout(f"No packet received within {timeout}s")


async def recv_packet(transport: Transport, timeout: float = 5.0) -> tuple[int, bytes]:
    """Receive and parse a COBS-framed packet from the device."""
    import time

    port = getattr(transport, '_port', None)
    if port is not None:
        return _recv_packet_sync(port, timeout)

    # Fallback: async transport
    frame = bytearray()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            data = await transport.read(256, timeout=min(remaining, 1.0))
            for byte in data:
                if byte == 0x00:
                    if frame:
                        try:
                            return parse_packet(bytes(frame))
                        except ValueError:
                            pass
                        frame.clear()
                else:
                    frame.append(byte)
                    if len(frame) > MAX_PACKET_SIZE:
                        frame.clear()
        except TransportTimeout:
            continue

    raise TransportTimeout(f"No packet received within {timeout}s")


async def recv_response(transport: Transport, timeout: float = 5.0) -> tuple[int, bytes]:
    """Receive a response packet, skipping stale READY packets.

    Timeout resets after each READY skip — READY packets are expected
    background traffic and shouldn't consume the timeout budget.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        cmd, data = await recv_packet(transport, remaining)
        if cmd != RSP_READY:
            return cmd, data
        # Reset deadline after READY — don't let background READYs
        # consume the timeout meant for the actual response
        deadline = time.monotonic() + timeout
    raise TransportTimeout(f"No response within {timeout}s")


async def wait_for_ready(transport: Transport, timeout: float = 10.0) -> bool:
    """Wait for the agent to send its READY packet after boot."""
    import asyncio
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            cmd, data = await recv_packet(transport, remaining)
            if cmd == RSP_READY:
                await asyncio.sleep(0.1)
                try:
                    w = await transport.bytes_waiting()
                    if w > 0:
                        await transport.read(w, timeout=0.1)
                except Exception:
                    pass
                return True
        except (TransportTimeout, ValueError):
            continue
    return False
