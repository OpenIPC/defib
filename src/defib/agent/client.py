"""High-level client for communicating with the flash agent.

Uploads the agent binary via the existing boot protocol, then
communicates via the COBS binary protocol for fast flash operations.
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path
from typing import Callable

from defib.agent.protocol import (
    ACK_OK,
    CMD_CRC32,
    CMD_INFO,
    CMD_READ,
    CMD_REBOOT,
    CMD_SELFUPDATE,
    CMD_SET_BAUD,
    CMD_WRITE,
    RSP_ACK,
    RSP_CRC32,
    RSP_DATA,
    RSP_INFO,
    RSP_READY,
    recv_packet,
    recv_response,
    send_packet,
    wait_for_ready,
)
from defib.transport.base import Transport

logger = logging.getLogger(__name__)

# Max bytes per WRITE transfer before PL011 FIFO overflow on uncached DDR.
# Agent processes COBS+CRC between packets; at 115200 baud with no D-cache,
# cumulative FIFO overflow occurs after ~30-60KB of continuous streaming.
WRITE_CHUNK_SIZE = 512
WRITE_MAX_TRANSFER = 16 * 1024


def get_agent_binary(chip: str) -> Path | None:
    """Get the path to the pre-compiled agent binary for a chip."""
    chip_to_agent = {
        "hi3516ev300": "hi3516ev300",
        "hi3516ev200": "hi3516ev200",
        "hi3518ev300": "hi3516ev200",
        "gk7205v200": "gk7205v200",
        "gk7205v300": "gk7205v200",
        "gk7202v300": "gk7205v200",
        "hi3516cv300": "hi3516cv300",
        "hi3516cv500": "hi3516cv500",
        "hi3516cv610": "hi3516cv610",
        "hi3518ev200": "hi3518ev200",
    }

    agent_name = chip_to_agent.get(chip.lower())
    if not agent_name:
        return None

    candidates = [
        Path(__file__).parent.parent.parent.parent / "agent" / f"agent-{agent_name}.bin",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


class FlashAgentClient:
    """Client for the bare-metal flash agent.

    Usage:
        client = FlashAgentClient(transport, chip="hi3516ev300")
        if await client.connect():
            info = await client.get_info()
            data = await client.read_memory(0x41000000, 4096)
            await client.dump_memory("dump.bin", 0x40000000, 256*1024)
    """

    def __init__(self, transport: Transport, chip: str = "") -> None:
        self._transport = transport
        self._chip = chip
        self._connected = False
        self._flash_size = 0
        self._ram_base = 0
        self._sector_size = 0x10000

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def flash_size(self) -> int:
        return self._flash_size

    @property
    def ram_base(self) -> int:
        return self._ram_base

    async def connect(self, timeout: float = 10.0) -> bool:
        """Wait for agent READY packet."""
        self._connected = await wait_for_ready(self._transport, timeout)
        return self._connected

    async def get_info(self) -> dict[str, int | str]:
        """Request device info from the agent."""
        await send_packet(self._transport, CMD_INFO)
        cmd, data = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_INFO or len(data) < 16:
            return {}

        jedec = data[0:3]
        flash_size = struct.unpack("<I", data[4:8])[0]
        ram_base = struct.unpack("<I", data[8:12])[0]
        sector_size = struct.unpack("<I", data[12:16])[0]

        self._flash_size = flash_size
        self._ram_base = ram_base
        self._sector_size = sector_size

        return {
            "jedec_id": f"{jedec[0]:02x}{jedec[1]:02x}{jedec[2]:02x}",
            "flash_size": flash_size,
            "ram_base": ram_base,
            "sector_size": sector_size,
        }

    async def read_memory(
        self,
        addr: int,
        size: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> bytes:
        """Read a memory region from the device. Returns bytes."""
        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_READ, payload)

        received = bytearray()
        while True:
            cmd, data = await recv_packet(self._transport, timeout=60.0)
            if cmd == RSP_READY:
                continue
            elif cmd == RSP_DATA and len(data) > 2:
                received.extend(data[2:])
                if on_progress:
                    on_progress(len(received), size)
            elif cmd == RSP_ACK:
                break
            else:
                raise RuntimeError(f"Unexpected response: cmd=0x{cmd:02x}")

        return bytes(received)

    async def dump_memory(
        self,
        output_path: str,
        addr: int,
        size: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Read memory region and write to file. Returns bytes written."""
        data = await self.read_memory(addr, size, on_progress)
        Path(output_path).write_bytes(data)
        return len(data)

    async def write_memory(
        self,
        addr: int,
        data: bytes,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Write data to device RAM in chunked transfers.

        Splits into WRITE_MAX_TRANSFER-sized blocks to avoid PL011 FIFO
        overflow on uncached DDR. Each block uses WRITE_CHUNK_SIZE packets.
        """
        total = len(data)
        offset = 0

        while offset < total:
            block_size = min(WRITE_MAX_TRANSFER, total - offset)
            block = data[offset:offset + block_size]

            ok = await self._write_block(addr + offset, block)
            if not ok:
                logger.error("WRITE failed at offset %d/%d", offset, total)
                return False

            offset += block_size
            if on_progress:
                on_progress(offset, total)

        return True

    async def _write_block(self, addr: int, data: bytes) -> bool:
        """Write a single block (up to WRITE_MAX_TRANSFER) to device."""
        crc = zlib.crc32(data) & 0xFFFFFFFF
        payload = struct.pack("<III", addr, len(data), crc)
        await send_packet(self._transport, CMD_WRITE, payload)

        cmd, resp = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        offset = 0
        seq = 0
        while offset < len(data):
            chunk = min(WRITE_CHUNK_SIZE, len(data) - offset)
            pkt = struct.pack("<H", seq) + data[offset:offset + chunk]
            await send_packet(self._transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        cmd, resp = await recv_response(self._transport, timeout=60.0)
        return cmd == RSP_ACK and resp[0] == ACK_OK

    async def crc32(self, addr: int, size: int) -> int:
        """Get CRC32 of a memory region from the device."""
        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_CRC32, payload)
        cmd, data = await recv_response(self._transport, timeout=10.0)
        if cmd != RSP_CRC32 or len(data) < 4:
            raise RuntimeError("CRC32 response invalid")
        return int(struct.unpack("<I", data[:4])[0])

    async def verify(self, addr: int, data: bytes) -> bool:
        """Verify device memory matches local data via CRC32."""
        local_crc = zlib.crc32(data) & 0xFFFFFFFF
        device_crc = await self.crc32(addr, len(data))
        return local_crc == device_crc

    async def selfupdate(
        self,
        firmware: bytes,
        load_addr: int = 0x41000000,
    ) -> bool:
        """Update the running agent with new firmware.

        Sends data to staging area, verifies CRC, agent copies via
        trampoline and jumps to new code.
        """
        crc = zlib.crc32(firmware) & 0xFFFFFFFF
        payload = struct.pack("<III", load_addr, len(firmware), crc)
        await send_packet(self._transport, CMD_SELFUPDATE, payload)

        cmd, resp = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        offset = 0
        seq = 0
        while offset < len(firmware):
            chunk = min(1022, len(firmware) - offset)
            pkt = struct.pack("<H", seq) + firmware[offset:offset + chunk]
            await send_packet(self._transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        cmd, resp = await recv_response(self._transport, timeout=10.0)
        return cmd == RSP_ACK and resp[0] == ACK_OK

    async def set_baud(self, baud: int) -> bool:
        """Switch UART to a higher baud rate.

        Protocol: send SET_BAUD command, receive ACK at current baud,
        then both sides switch. Verifies with INFO at new baud.
        Falls back to original baud on failure.
        """
        import asyncio

        port = getattr(self._transport, '_port', None)
        if port is None:
            logger.error("set_baud requires serial transport with _port")
            return False

        old_baud = port.baudrate
        payload = struct.pack("<I", baud)
        await send_packet(self._transport, CMD_SET_BAUD, payload)

        cmd, resp = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            logger.error("Agent rejected baud rate %d", baud)
            return False

        # Agent has switched — now switch host side
        await asyncio.sleep(0.05)  # Brief pause for agent to complete switch
        port.baudrate = baud

        # Verify communication at new baud
        await asyncio.sleep(0.05)
        try:
            await send_packet(self._transport, CMD_INFO)
            cmd, data = await recv_response(self._transport, timeout=3.0)
            if cmd == RSP_INFO:
                logger.info("Baud rate switched to %d", baud)
                return True
        except Exception:
            pass

        # Failed — switch back
        logger.warning("Verification at %d baud failed, reverting to %d", baud, old_baud)
        port.baudrate = old_baud
        return False

    async def reboot(self) -> None:
        """Tell the agent to reset the device."""
        await send_packet(self._transport, CMD_REBOOT)
