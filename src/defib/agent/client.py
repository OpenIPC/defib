"""High-level client for communicating with the flash agent.

Uploads the agent binary via the existing boot protocol, then
communicates via the COBS binary protocol for fast flash operations.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable

from defib.agent.protocol import (
    ACK_OK,
    CMD_CRC32,
    CMD_ERASE,
    CMD_FLASH_STREAM,
    CMD_INFO,
    CMD_MARK_BAD,
    CMD_READ,
    CMD_SCAN,
    CMD_SELFUPDATE,
    CMD_SET_BAUD,
    CMD_WRITE,
    RSP_ACK,
    RSP_CRC32,
    RSP_DATA,
    RSP_INFO,
    RSP_READY,
    RSP_SCAN,
    recv_packet,
    recv_response,
    send_packet,
    wait_for_ready,
)
from defib.transport.base import Transport

logger = logging.getLogger(__name__)


class SectorStatus(IntEnum):
    GOOD = 0x00
    EMPTY = 0x01
    STUCK_ZERO = 0x02
    STUCK_PATTERN = 0x03
    UNSTABLE = 0x04
    READ_ERROR = 0x05
    BAD_BLOCK = 0x06   # NAND only: factory-marked bad block (OOB[0] != 0xFF)


@dataclass
class SectorResult:
    index: int
    address: int
    status: SectorStatus
    crc32: int


@dataclass
class ScanResult:
    sectors: list[SectorResult] = field(default_factory=list)
    flash_size: int = 0
    sector_size: int = 0

    @property
    def total(self) -> int:
        return len(self.sectors)

    @property
    def good(self) -> list[SectorResult]:
        return [s for s in self.sectors if s.status == SectorStatus.GOOD]

    @property
    def empty(self) -> list[SectorResult]:
        return [s for s in self.sectors if s.status == SectorStatus.EMPTY]

    @property
    def bad(self) -> list[SectorResult]:
        return [s for s in self.sectors if s.status in (
            SectorStatus.STUCK_ZERO, SectorStatus.STUCK_PATTERN,
            SectorStatus.READ_ERROR,
        )]

    @property
    def unstable(self) -> list[SectorResult]:
        return [s for s in self.sectors if s.status == SectorStatus.UNSTABLE]

    @property
    def bad_block(self) -> list[SectorResult]:
        return [s for s in self.sectors if s.status == SectorStatus.BAD_BLOCK]


# Packet size for WRITE data chunks
WRITE_CHUNK_SIZE = 512
# Max bytes per WRITE block.
WRITE_MAX_TRANSFER = 16 * 1024
# Number of blocks sent before reading ACKs (windowed ACK).
# Higher = less round-trip overhead, but more data at risk on failure.
WRITE_WINDOW = 16  # 16 × 16KB = 256KB in flight

# Optimal baud rate from hi3516ev300 + FT232R benchmarks.
# 921600 is the highest rate verified for both READ and WRITE with
# CRC32 integrity on real hardware. 1152000 works for READ-only but
# WRITE is marginal. Single rate avoids unnecessary switching.
DEFAULT_FAST_BAUD = 921600    # ~82 KB/s both directions
FALLBACK_BAUD = 115200        # Always works


def get_agent_binary(chip: str) -> Path | None:
    """Get the path to the pre-compiled agent binary for a chip."""
    chip_to_agent = {
        "hi3516ev300": "hi3516ev300",
        "gk7205v200": "gk7205v200",
        "gk7205v300": "gk7205v200",
        "gk7202v300": "gk7205v200",
        "hi3516cv300": "hi3516cv300",
        "hi3516cv500": "hi3516cv500",
        "hi3516av300": "hi3516cv500",  # cv500-family, same memory map
        "hi3516dv300": "hi3516cv500",  # cv500-family, same memory map
        "hi3519v101": "hi3519v101",
        "hi3516av200": "hi3519v101",   # 3519v101 family, same memory map
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
        self._current_baud = FALLBACK_BAUD

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

    def _clear_rx_buffers(self) -> None:
        """Clear stale parsed data from _port_buffers."""
        from defib.agent.protocol import _port_buffers
        port = getattr(self._transport, '_port', None)
        if port is not None:
            _port_buffers[id(port)] = bytearray()

    async def _switch_baud(self, target: int) -> bool:
        """Switch to target baud if not already there. Returns success."""
        if self._current_baud == target:
            return True
        ok = await self.set_baud(target)
        if ok:
            self._current_baud = target
        return ok

    async def _restore_baud(self) -> None:
        """Return to fallback baud rate."""
        if self._current_baud != FALLBACK_BAUD:
            ok = await self.set_baud(FALLBACK_BAUD)
            if ok:
                self._current_baud = FALLBACK_BAUD

    # Agent capability flags (must match agent/main.c)
    CAP_FLASH_STREAM = 1 << 0
    CAP_SECTOR_BITMAP = 1 << 1
    CAP_PAGE_SKIP = 1 << 2
    CAP_SET_BAUD = 1 << 3
    CAP_REBOOT = 1 << 4
    CAP_SELFUPDATE = 1 << 5
    CAP_SCAN = 1 << 6

    async def get_info(self) -> dict[str, int | str]:
        """Request device info from the agent."""
        self._clear_rx_buffers()
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

        result: dict[str, int | str] = {
            "jedec_id": f"{jedec[0]:02x}{jedec[1]:02x}{jedec[2]:02x}",
            "flash_size": flash_size,
            "ram_base": ram_base,
            "sector_size": sector_size,
        }

        # Extended fields (agent version >= 2)
        if len(data) >= 24:
            version = struct.unpack("<I", data[16:20])[0]
            caps = struct.unpack("<I", data[20:24])[0]
            result["agent_version"] = version
            result["capabilities"] = caps

        return result

    async def read_memory(
        self,
        addr: int,
        size: int,
        on_progress: Callable[[int, int], None] | None = None,
        fast: bool = True,
    ) -> bytes:
        """Read a memory region from the device. Returns bytes.

        If fast=True and size > 4KB, switches to DEFAULT_FAST_BAUD,
        falling back to FALLBACK_BAUD if the switch fails.
        """
        self._clear_rx_buffers()

        if fast and size > 4096:
            await self._switch_baud(DEFAULT_FAST_BAUD)

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
        fast: bool = True,
    ) -> bool:
        """Write data to device RAM using windowed ACK.

        Sends W blocks (CMD_WRITE + DATA stream) back-to-back without
        waiting for ACKs, then reads all W ACKs. This pipelines the
        round-trips, approaching line speed.

        Falls back to single-block mode on failure.
        """
        self._clear_rx_buffers()

        if fast and len(data) > 4096:
            await self._switch_baud(DEFAULT_FAST_BAUD)

        total = len(data)
        offset = 0
        window = min(WRITE_WINDOW, (total + WRITE_MAX_TRANSFER - 1) // WRITE_MAX_TRANSFER)

        while offset < total:
            # Send a window of blocks without waiting for ACKs
            blocks_sent = 0
            window_start = offset

            for _ in range(window):
                if offset >= total:
                    break
                block_size = min(WRITE_MAX_TRANSFER, total - offset)
                block = data[offset:offset + block_size]
                crc = zlib.crc32(block) & 0xFFFFFFFF

                # Send CMD_WRITE + all DATA packets (no wait)
                payload = struct.pack("<III", addr + offset, block_size, crc)
                await send_packet(self._transport, CMD_WRITE, payload)

                blk_off = 0
                seq = 0
                while blk_off < block_size:
                    chunk = min(WRITE_CHUNK_SIZE, block_size - blk_off)
                    pkt = struct.pack("<H", seq) + block[blk_off:blk_off + chunk]
                    await send_packet(self._transport, RSP_DATA, pkt)
                    blk_off += chunk
                    seq += 1

                offset += block_size
                blocks_sent += 1

            # Now read all ACKs: initial ACK + CRC ACK per block
            all_ok = True
            for i in range(blocks_sent):
                try:
                    # Initial ACK
                    cmd, resp = await recv_response(self._transport, timeout=10.0)
                    if cmd != RSP_ACK or resp[0] != ACK_OK:
                        all_ok = False
                        break

                    # CRC ACK
                    crc_timeout = max(60.0, WRITE_MAX_TRANSFER / 50000)
                    cmd, resp = await recv_response(self._transport, timeout=crc_timeout)
                    if cmd != RSP_ACK or resp[0] != ACK_OK:
                        all_ok = False
                        break
                except Exception:
                    all_ok = False
                    break

                if on_progress:
                    on_progress(window_start + (i + 1) * WRITE_MAX_TRANSFER, total)

            if not all_ok:
                # Fall back to single-block retry for the failed window
                logger.warning("Window failed at offset %d, retrying single-block", window_start)
                offset = window_start
                window = 1  # Degrade to single-block mode
                self._clear_rx_buffers()

                # Drain any stale ACKs from partially-completed window
                import asyncio
                await asyncio.sleep(0.5)
                port = getattr(self._transport, '_port', None)
                if port is not None:
                    port.reset_input_buffer()
                    from defib.agent.protocol import _port_buffers
                    _port_buffers[id(port)] = bytearray()

                # Retry this window as single blocks
                for _ in range(blocks_sent):
                    if offset >= total:
                        break
                    block_size = min(WRITE_MAX_TRANSFER, total - offset)
                    block = data[offset:offset + block_size]
                    ok = await self._write_block(addr + offset, block)
                    if not ok:
                        logger.error("Single-block retry failed at %d/%d", offset, total)
                        return False
                    offset += block_size
                    if on_progress:
                        on_progress(offset, total)

                # Restore window for next iteration
                window = min(WRITE_WINDOW, (total - offset + WRITE_MAX_TRANSFER - 1) // WRITE_MAX_TRANSFER)

        return True

    async def _write_block(self, addr: int, data: bytes) -> bool:
        """Write a single block (up to WRITE_MAX_TRANSFER) to device."""
        self._clear_rx_buffers()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        payload = struct.pack("<III", addr, len(data), crc)
        await send_packet(self._transport, CMD_WRITE, payload)

        cmd, resp = await recv_response(self._transport, timeout=10.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        # Stream all DATA packets without per-packet ACK.
        # With COBS bug fixed + D-cache, the agent processes fast
        # enough to keep up at 921600 baud.
        offset = 0
        seq = 0
        while offset < len(data):
            chunk = min(WRITE_CHUNK_SIZE, len(data) - offset)
            pkt = struct.pack("<H", seq) + data[offset:offset + chunk]
            await send_packet(self._transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        # Final CRC verification ACK — may take seconds for large transfers
        crc_timeout = max(60.0, len(data) / 50000)  # ~20µs/byte for CRC32
        cmd, resp = await recv_response(self._transport, timeout=crc_timeout)
        return cmd == RSP_ACK and resp[0] == ACK_OK

    async def erase_flash(
        self,
        addr: int,
        size: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Erase flash sectors. addr and size must be sector-aligned."""
        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_ERASE, payload)

        cmd, resp = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        sector_sz = self._sector_size or 0x10000
        num_sectors = size // sector_sz

        # Receive progress packets (RSP_DATA with sectors_done count)
        for _ in range(num_sectors):
            cmd, data = await recv_packet(self._transport, timeout=30.0)
            if cmd == RSP_READY:
                continue
            if cmd == RSP_DATA and len(data) >= 2:
                done = data[0] | (data[1] << 8)
                if on_progress:
                    on_progress(done * sector_sz, size)

        # Final ACK
        cmd, resp = await recv_response(self._transport, timeout=10.0)
        return cmd == RSP_ACK and resp[0] == ACK_OK

    async def write_flash(
        self,
        addr: int,
        data: bytes,
        on_progress: Callable[[int, int], None] | None = None,
        fast: bool = True,
    ) -> bool:
        """Stream data directly to flash: erase + receive + program per sector.

        Single-phase: host streams DATA, agent erases/programs each sector
        as data arrives. No separate RAM upload. Fastest possible path.

        If fast=True and data > 4KB, switches to DEFAULT_FAST_BAUD before
        streaming. Without this, multi-MiB writes (e.g. agent flash) run
        at the boot baud (~12 KB/s) instead of ~85 KB/s.
        """
        self._clear_rx_buffers()

        if fast and len(data) > 4096:
            await self._switch_baud(DEFAULT_FAST_BAUD)

        total = len(data)
        expected_crc = zlib.crc32(data) & 0xFFFFFFFF
        sector_sz = self._sector_size or 0x10000
        num_sectors = (total + sector_sz - 1) // sector_sz

        # Build sector bitmap: bit=1 if sector has non-0xFF data
        ff_sector = b'\xff' * sector_sz
        bitmap = bytearray(32)
        for s in range(num_sectors):
            sector_data = data[s * sector_sz : (s + 1) * sector_sz]
            if sector_data != ff_sector[:len(sector_data)]:
                bitmap[s // 8] |= 1 << (s % 8)

        skip_count = num_sectors - bin(int.from_bytes(bitmap, 'little')).count('1')
        if skip_count > 0:
            logger.info("Flash stream: skipping %d/%d sectors (0xFF)",
                        skip_count, num_sectors)

        payload = struct.pack("<III", addr, total, expected_crc) + bytes(bitmap)
        await send_packet(self._transport, CMD_FLASH_STREAM, payload)

        cmd, resp = await recv_response(self._transport, timeout=10.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            logger.error("Flash stream rejected: 0x%02x", resp[0] if resp else -1)
            return False

        # Double-buffer pipeline: send sector N, get "received" signal,
        # immediately send sector N+1 while agent erases+programs N.
        # Sectors with bitmap bit=0 are skipped (all 0xFF).
        offset = 0

        for s in range(num_sectors):
            sector_bytes = min(sector_sz, total - offset)

            if bitmap[s // 8] & (1 << (s % 8)):
                # Send one sector of DATA packets
                sent = 0
                seq = offset // WRITE_CHUNK_SIZE
                while sent < sector_bytes:
                    chunk = min(WRITE_CHUNK_SIZE, sector_bytes - sent)
                    pkt = struct.pack("<H", seq) + data[offset + sent:offset + sent + chunk]
                    await send_packet(self._transport, RSP_DATA, pkt)
                    sent += chunk
                    seq += 1

            # else: sector is all 0xFF — don't send data, agent skips it

            offset += sector_bytes

            # Wait for progress — agent sends RSP_DATA for both data and
            # skipped sectors (immediately for skipped, after receive for data).
            while True:
                cmd, data_pkt = await recv_packet(self._transport, timeout=120.0)
                if cmd == RSP_READY:
                    continue
                elif cmd == RSP_DATA:
                    done = data_pkt[0] | (data_pkt[1] << 8)
                    total_steps = data_pkt[2] | (data_pkt[3] << 8)
                    if on_progress and total_steps > 0:
                        on_progress(total * done // total_steps, total)
                    break
                elif cmd == RSP_ACK:
                    if data_pkt[0] == ACK_OK:
                        return True
                    logger.error("Flash stream failed: 0x%02x", data_pkt[0])
                    return False
                else:
                    return False

        # Wait for final ACK (agent finishes programming last sector)
        while True:
            cmd, data_pkt = await recv_packet(self._transport, timeout=120.0)
            if cmd == RSP_READY:
                continue
            elif cmd == RSP_ACK:
                return data_pkt[0] == ACK_OK
            elif cmd == RSP_DATA:
                continue  # Late progress packet
            else:
                break

        return False

    async def crc32(self, addr: int, size: int) -> int:
        """Get CRC32 of a memory region from the device."""
        self._clear_rx_buffers()

        payload = struct.pack("<II", addr, size)
        await send_packet(self._transport, CMD_CRC32, payload)
        timeout = max(10.0, 5.0 + size / (1024 * 1024))
        cmd, data = await recv_response(self._transport, timeout=timeout)
        if cmd != RSP_CRC32 or len(data) < 4:
            raise RuntimeError("CRC32 response invalid")
        return int(struct.unpack("<I", data[:4])[0])

    async def verify(self, addr: int, data: bytes) -> bool:
        """Verify device memory matches local data via CRC32."""
        local_crc = zlib.crc32(data) & 0xFFFFFFFF
        device_crc = await self.crc32(addr, len(data))
        return local_crc == device_crc

    async def scan_flash(
        self,
        on_progress: Callable[[int, int], None] | None = None,
        on_sector: Callable[[SectorResult], None] | None = None,
    ) -> ScanResult:
        """Scan entire flash for bad/unstable sectors.

        Uses CMD_SCAN for efficient agent-side scanning. Falls back to
        per-sector CRC32 if the agent doesn't support CMD_SCAN.
        Calls on_sector() as each result is decoded for live UI updates.
        """
        self._clear_rx_buffers()

        await self._switch_baud(DEFAULT_FAST_BAUD)

        await send_packet(self._transport, CMD_SCAN)

        cmd, data = await recv_response(self._transport, timeout=10.0)

        # Old agents respond with ACK_CRC_ERROR for unknown commands
        if cmd == RSP_ACK:
            logger.info("Agent doesn't support CMD_SCAN, using CRC32 fallback")
            return await self._scan_flash_compat(on_progress, on_sector)

        if cmd != RSP_SCAN or len(data) < 4:
            raise RuntimeError(f"Unexpected scan header: cmd=0x{cmd:02x}")

        num_sectors = struct.unpack("<I", data[:4])[0]
        sectors: list[SectorResult] = []

        while len(sectors) < num_sectors:
            cmd, data = await recv_packet(self._transport, timeout=30.0)
            if cmd == RSP_READY:
                continue
            if cmd == RSP_ACK:
                break
            if cmd != RSP_SCAN:
                raise RuntimeError(f"Unexpected response during scan: 0x{cmd:02x}")

            offset = 0
            while offset + 5 <= len(data) and len(sectors) < num_sectors:
                status_byte = data[offset]
                crc_val = struct.unpack("<I", data[offset + 1:offset + 5])[0]
                idx = len(sectors)
                try:
                    status = SectorStatus(status_byte)
                except ValueError:
                    status = SectorStatus.READ_ERROR
                result = SectorResult(
                    index=idx,
                    address=idx * self._sector_size,
                    status=status,
                    crc32=crc_val,
                )
                sectors.append(result)
                if on_sector:
                    on_sector(result)
                if on_progress:
                    on_progress(len(sectors), num_sectors)
                offset += 5

        # Consume final ACK if we haven't already
        if cmd != RSP_ACK:
            cmd, data = await recv_response(self._transport, timeout=5.0)

        return ScanResult(
            sectors=sectors,
            flash_size=self._flash_size,
            sector_size=self._sector_size,
        )

    async def _scan_flash_compat(
        self,
        on_progress: Callable[[int, int], None] | None = None,
        on_sector: Callable[[SectorResult], None] | None = None,
    ) -> ScanResult:
        """Fallback scan using per-sector CRC32 commands."""
        sector_sz = self._sector_size or 0x10000
        flash_sz = self._flash_size
        if flash_sz == 0:
            info = await self.get_info()
            flash_sz = int(info.get("flash_size", 0))
        num_sectors = flash_sz // sector_sz if sector_sz else 0

        # Pre-compute CRC32 of an empty sector for comparison
        empty_crc = zlib.crc32(b"\xFF" * sector_sz) & 0xFFFFFFFF

        sectors: list[SectorResult] = []
        flash_base = 0x14000000  # FLASH_MEM

        for i in range(num_sectors):
            addr = flash_base + i * sector_sz

            # Pass 1
            try:
                crc1 = await self.crc32(addr, sector_sz)
            except Exception:
                result = SectorResult(i, i * sector_sz, SectorStatus.READ_ERROR, 0)
                sectors.append(result)
                if on_sector:
                    on_sector(result)
                if on_progress:
                    on_progress(len(sectors), num_sectors)
                continue

            if crc1 == empty_crc:
                status = SectorStatus.EMPTY
            else:
                # Pass 2: stability check
                try:
                    crc2 = await self.crc32(addr, sector_sz)
                except Exception:
                    crc2 = crc1
                status = SectorStatus.UNSTABLE if crc2 != crc1 else SectorStatus.GOOD

            result = SectorResult(i, i * sector_sz, status, crc1)
            sectors.append(result)
            if on_sector:
                on_sector(result)
            if on_progress:
                on_progress(len(sectors), num_sectors)

        return ScanResult(
            sectors=sectors,
            flash_size=flash_sz,
            sector_size=sector_sz,
        )

    async def selfupdate(
        self,
        firmware: bytes,
        load_addr: int = 0x41000000,
    ) -> bool:
        """Update the running agent with new firmware.

        Sends data to staging area with per-packet backpressure ACK,
        verifies CRC, agent copies via trampoline and jumps to new code.
        After jump, reconnects and verifies the binary matches.
        """
        self._clear_rx_buffers()

        crc = zlib.crc32(firmware) & 0xFFFFFFFF
        payload = struct.pack("<III", load_addr, len(firmware), crc)
        await send_packet(self._transport, CMD_SELFUPDATE, payload)

        cmd, resp = await recv_response(self._transport, timeout=5.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            return False

        # Stream all data packets
        offset = 0
        seq = 0
        while offset < len(firmware):
            chunk = min(WRITE_CHUNK_SIZE, len(firmware) - offset)
            pkt = struct.pack("<H", seq) + firmware[offset:offset + chunk]
            await send_packet(self._transport, RSP_DATA, pkt)
            offset += chunk
            seq += 1

        # Wait for CRC verification ACK (agent verifies before jumping)
        cmd, resp = await recv_response(self._transport, timeout=30.0)
        if cmd != RSP_ACK or resp[0] != ACK_OK:
            logger.error("Selfupdate CRC failed: 0x%02x", resp[0])
            return False

        return True

    async def set_baud(self, baud: int) -> bool:
        """Switch UART to a higher baud rate.

        Protocol: send SET_BAUD command, receive ACK at current baud,
        then both sides switch. Verifies with INFO at new baud.
        Falls back to original baud on failure.
        """
        self._clear_rx_buffers()

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

    async def mark_bad_block(self, block: int) -> bool:
        """Mark a NAND block as bad by writing 0x00 to OOB[0] of page 0.

        After this call, scan_flash() will report the block as
        SectorStatus.BAD_BLOCK.  An erase_flash() of the block clears
        the marker (OOB returns to 0xFF after erase).

        Used for testing the scan's bad-block detection.  NOR returns
        False (no OOB).
        """
        self._clear_rx_buffers()
        await send_packet(self._transport, CMD_MARK_BAD, struct.pack("<I", block))
        cmd, resp = await recv_response(self._transport, timeout=5.0)
        return cmd == RSP_ACK and resp[0] == ACK_OK

    async def reboot(self) -> None:
        """Reset the device via watchdog. Bootrom boots from flash if
        valid firmware is present, otherwise enters serial download."""
        self._clear_rx_buffers()
        from defib.agent.protocol import CMD_REBOOT
        await send_packet(self._transport, CMD_REBOOT)
        try:
            cmd, resp = await recv_response(self._transport, timeout=5.0)
        except Exception:
            pass  # Agent may reset before ACK arrives
