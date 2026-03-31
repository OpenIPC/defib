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

Responses (device → host):
  0x81  INFO_RSP — chip_id(4B) + flash_size(4B) + ram_base(4B)
  0x82  DATA     — seq(2B LE) + data(up to 1024B)
  0x83  ACK      — status(1B): 0=OK, 1=CRC error, 2=flash error
  0x84  CRC32_RSP— crc32(4B LE)
  0x85  READY    — Agent is running and ready
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

# Responses
RSP_INFO = 0x81
RSP_DATA = 0x82
RSP_ACK = 0x83
RSP_CRC32 = 0x84
RSP_READY = 0x85

# ACK status codes
ACK_OK = 0x00
ACK_CRC_ERROR = 0x01
ACK_FLASH_ERROR = 0x02

FRAME_DELIMITER = b"\x00"
MAX_PACKET_SIZE = 1100  # 1024 payload + header + CRC + COBS overhead


def build_packet(cmd: int, data: bytes = b"") -> bytes:
    """Build a COBS-framed packet with CRC32."""
    payload = bytes([cmd]) + data
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    payload += struct.pack("<I", crc)
    encoded = cobs.encode(payload)
    return encoded + FRAME_DELIMITER


def parse_packet(raw: bytes) -> tuple[int, bytes]:
    """Parse a COBS-framed packet, verify CRC32.

    Returns (command_byte, data_without_crc).
    Raises ValueError on CRC mismatch or decode error.
    """
    decoded = cobs.decode(raw)
    if len(decoded) < 5:  # 1 cmd + 4 CRC minimum
        raise ValueError(f"Packet too short: {len(decoded)} bytes")
    payload = decoded[:-4]
    expected_crc = struct.unpack("<I", decoded[-4:])[0]
    actual_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ValueError(
            f"CRC mismatch: expected {expected_crc:08x}, got {actual_crc:08x}"
        )
    return payload[0], payload[1:]


async def send_packet(transport: Transport, cmd: int, data: bytes = b"") -> None:
    """Send a COBS-framed packet to the device."""
    packet = build_packet(cmd, data)
    # Use direct pyserial when available for consistent timing
    port = getattr(transport, '_port', None)
    if port is not None:
        port.write(packet)
    else:
        await transport.write(packet)


# Per-port read buffer — survives across recv_packet calls so bytes
# read past the first 0x00 delimiter aren't lost.
_port_buffers: dict[int, bytearray] = {}


def _get_port_buf(port: object) -> bytearray:
    key = id(port)
    if key not in _port_buffers:
        _port_buffers[key] = bytearray()
    return _port_buffers[key]


async def recv_packet(transport: Transport, timeout: float = 5.0) -> tuple[int, bytes]:
    """Receive and parse a COBS-framed packet from the device.

    Reads chunks until 0x00 delimiter found, then COBS-decodes and verifies CRC.
    Uses direct pyserial access when available for reliable timing.
    """
    import time

    # Try direct pyserial access (bypasses async executor overhead)
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


def _recv_packet_sync(port: object, timeout: float) -> tuple[int, bytes]:
    """Synchronous recv using pyserial directly.

    Uses a per-port persistent buffer so bytes read past the first
    complete packet aren't lost between calls.
    """
    import time
    portbuf = _get_port_buf(port)
    frame = bytearray()
    old_timeout = port.timeout  # type: ignore[union-attr]
    deadline = time.monotonic() + timeout

    def _process_byte(b: int) -> tuple[int, bytes] | None:
        """Process one byte. Returns parsed packet or None."""
        if b == 0x00:
            if frame:
                try:
                    result = parse_packet(bytes(frame))
                    frame.clear()
                    return result
                except ValueError:
                    pass
                frame.clear()
        else:
            frame.append(b)
            if len(frame) > MAX_PACKET_SIZE:
                frame.clear()
        return None

    def _drain(source: bytes | bytearray) -> tuple[int, bytes] | None:
        for i, byte in enumerate(source):
            result = _process_byte(byte)
            if result is not None:
                # Stash remaining unprocessed bytes
                portbuf.extend(source[i + 1:])
                return result
        return None

    try:
        # First drain any leftover bytes from previous call
        if portbuf:
            leftover = bytes(portbuf)
            portbuf.clear()
            result = _drain(leftover)
            if result is not None:
                return result

        while time.monotonic() < deadline:
            waiting = port.in_waiting  # type: ignore[union-attr]
            if waiting > 0:
                data = port.read(waiting)  # type: ignore[union-attr]
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                port.timeout = min(remaining, 0.1)  # type: ignore[union-attr]
                data = port.read(1)  # type: ignore[union-attr]
            if data:
                result = _drain(data)
                if result is not None:
                    return result
    finally:
        port.timeout = old_timeout  # type: ignore[union-attr]

    raise TransportTimeout(f"No packet received within {timeout}s")


async def recv_response(transport: Transport, timeout: float = 5.0) -> tuple[int, bytes]:
    """Receive a response packet, skipping stale READY packets."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        cmd, data = await recv_packet(transport, remaining)
        if cmd != RSP_READY:
            return cmd, data
    raise TransportTimeout(f"No response within {timeout}s")


async def wait_for_ready(transport: Transport, timeout: float = 10.0) -> bool:
    """Wait for the agent to send its READY packet after boot.

    Keeps reading packets until READY is found or timeout expires.
    After receiving READY, drains any extra buffered data.
    """
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
                # Drain extra READY re-sends
                await asyncio.sleep(0.1)
                try:
                    w = await transport.bytes_waiting()
                    if w > 0:
                        await transport.read(w, timeout=0.1)
                except Exception:
                    pass
                return True
            # Got a non-READY packet — skip it, keep waiting
        except (TransportTimeout, ValueError):
            continue
    return False
