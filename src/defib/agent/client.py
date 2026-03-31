"""High-level client for communicating with the flash agent.

Uploads the agent binary via the existing boot protocol, then
communicates via the COBS binary protocol for fast flash operations.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable

from defib.agent.protocol import (
    CMD_CRC32,
    CMD_ERASE,
    CMD_INFO,
    CMD_READ,
    CMD_REBOOT,
    CMD_WRITE,
    RSP_ACK,
    RSP_CRC32,
    RSP_DATA,
    RSP_INFO,
    ACK_OK,
    recv_packet,
    send_packet,
    wait_for_ready,
)
from defib.transport.base import Transport


def get_agent_binary(chip: str) -> Path | None:
    """Get the path to the pre-compiled agent binary for a chip.

    Looks in the agent/ directory of the defib repo for
    agent-{soc_family}.bin files.
    """
    # Map chip names to agent binary names
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

    # Look in the agent/ directory relative to the repo root
    # In installed package, these would be in package data
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
            await client.read_flash("dump.bin", on_progress=callback)
    """

    def __init__(self, transport: Transport, chip: str = "") -> None:
        self._transport = transport
        self._chip = chip
        self._connected = False
        self._flash_size = 0
        self._sector_size = 0x10000

    async def connect(self, timeout: float = 10.0) -> bool:
        """Wait for agent READY packet."""
        self._connected = await wait_for_ready(self._transport, timeout)
        return self._connected

    async def get_info(self) -> dict[str, int]:
        """Request device info from the agent."""
        await send_packet(self._transport, CMD_INFO)
        cmd, data = await recv_packet(self._transport, timeout=5.0)
        if cmd != RSP_INFO or len(data) < 16:
            return {}

        jedec = data[0:3]
        flash_size = struct.unpack("<I", data[4:8])[0]
        ram_base = struct.unpack("<I", data[8:12])[0]
        sector_size = struct.unpack("<I", data[12:16])[0]

        self._flash_size = flash_size
        self._sector_size = sector_size

        return {
            "jedec_id": f"{jedec[0]:02x}{jedec[1]:02x}{jedec[2]:02x}",
            "flash_size": flash_size,
            "ram_base": ram_base,
            "sector_size": sector_size,
        }

    async def read_flash(
        self,
        output_path: str,
        addr: int = 0,
        size: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Read flash contents to a file via the agent.

        Returns number of bytes read.
        """
        if size is None:
            size = self._flash_size
        if size == 0:
            raise RuntimeError("Flash size unknown — call get_info() first")

        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_READ, payload)

        bytes_received = 0
        with open(output_path, "wb") as f:
            while bytes_received < size:
                cmd, data = await recv_packet(self._transport, timeout=10.0)
                if cmd == RSP_DATA and len(data) > 2:
                    # seq(2B) + payload
                    chunk = data[2:]
                    f.write(chunk)
                    bytes_received += len(chunk)
                    if on_progress:
                        on_progress(bytes_received, size)
                elif cmd == RSP_ACK:
                    # Transfer complete
                    break
                else:
                    raise RuntimeError(f"Unexpected response: cmd=0x{cmd:02x}")

        return bytes_received

    async def crc32_flash(self, addr: int, size: int) -> int:
        """Get CRC32 of a flash region from the agent."""
        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_CRC32, payload)
        cmd, data = await recv_packet(self._transport, timeout=10.0)
        if cmd != RSP_CRC32 or len(data) < 4:
            raise RuntimeError("CRC32 response invalid")
        return int(struct.unpack("<I", data[:4])[0])

    async def verify_dump(self, file_path: str, flash_addr: int = 0) -> bool:
        """Verify a dump file against flash CRC32.

        Reads the file, computes local CRC32, requests device CRC32,
        compares.
        """
        data = Path(file_path).read_bytes()
        local_crc = zlib.crc32(data) & 0xFFFFFFFF
        device_crc = await self.crc32_flash(flash_addr, len(data))
        return local_crc == device_crc

    async def write_flash(
        self,
        data: bytes,
        addr: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Write data to flash (erase + program).

        Erases sectors first, then writes page by page.
        """
        total = len(data)

        # Erase required sectors
        erase_size = ((total + self._sector_size - 1) // self._sector_size) * self._sector_size
        erase_payload = struct.pack("<II", addr, erase_size)
        await send_packet(self._transport, CMD_ERASE, erase_payload)
        cmd, resp = await recv_packet(self._transport, timeout=60.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        # Write in 1KB chunks (max packet payload)
        offset = 0
        while offset < total:
            chunk_size = min(1020, total - offset)  # 1024 - 4 for addr
            chunk = data[offset:offset + chunk_size]
            payload = struct.pack("<I", addr + offset) + chunk
            await send_packet(self._transport, CMD_WRITE, payload)

            cmd, resp = await recv_packet(self._transport, timeout=10.0)
            if cmd != RSP_ACK or resp[0] != ACK_OK:
                return False

            offset += chunk_size
            if on_progress:
                on_progress(offset, total)

        return True

    async def reboot(self) -> None:
        """Tell the agent to reset the device."""
        await send_packet(self._transport, CMD_REBOOT)
