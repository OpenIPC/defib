"""SPI NAND study operations over the flash agent (CMD_NAND).

The agent drives the HiSilicon/Goke FMC100 controller's page engine the
way the Linux hifmc100 / xmedia_fmc100 drivers do — or deliberately not:
ECC type, transfer mode, SPI opcodes and the exact OOB bytes are all the
caller's.  That is what it takes to chase ECC faults such as
OpenIPC/firmware#2285 and #2519 below the OS.

Pages and per-page results stay in agent RAM (``NandInfo.dma_buf`` /
``stat_buf``) and move over the ordinary CMD_READ / CMD_WRITE stream;
CMD_NAND itself only carries parameters and one-record answers.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from defib.agent.protocol import (
    CMD_NAND,
    RSP_ACK,
    RSP_DATA,
    RSP_NAND,
    recv_packet,
    recv_response,
    send_packet,
)
from defib.transport.base import TransportError, TransportTimeout

if TYPE_CHECKING:
    from defib.agent.client import FlashAgentClient

# Sub-operations and statuses (agent/protocol.h).
NAND_OP_INFO = 0x00
NAND_OP_FEATURE_GET = 0x01
NAND_OP_FEATURE_SET = 0x02
NAND_OP_READ_PAGES = 0x03
NAND_OP_PROGRAM_PAGE = 0x04
NAND_OP_ERASE_BLOCK = 0x05
NAND_OP_FMC_REG = 0x06

NAND_ST_OK = 0x00
NAND_ST_BADARG = 0x01
NAND_ST_NOT_NAND = 0x02
NAND_ST_IO = 0x03

REC_SIZE = 8
REC_TIMEOUT = 1 << 0
REC_FAIL = 1 << 1

# SPI NAND feature registers.
FEATURE_PROTECT = 0xA0
FEATURE_CONFIG = 0xB0   # bit 4 ECC-E (on-die ECC), bit 3 BUF on Winbond
FEATURE_STATUS = 0xC0   # bits 5:4 ECC_S, 3 P_FAIL, 2 E_FAIL, 0 OIP

# FMC_CFG fields (include/linux/mfd/hisi_fmc.h).
FMC_CFG_OP_MODE_NORMAL = 1 << 0
FMC_CFG_FLASH_SEL_MASK = 0x3 << 1
FMC_CFG_FLASH_SEL_SPI_NAND = 0x1 << 1
FMC_CFG_PAGE_SIZE_MASK = 0x3 << 3
FMC_CFG_ECC_TYPE_SHIFT = 5
FMC_CFG_ECC_TYPE_MASK = 0x7 << FMC_CFG_ECC_TYPE_SHIFT
FMC_CFG_BLOCK_SIZE_MASK = 0x3 << 8


class EccType(IntEnum):
    """FMC_CFG ECC_TYPE values."""

    NONE = 0
    BIT8 = 1
    BIT16 = 2
    BIT24 = 3
    BIT28 = 4
    BIT40 = 5
    BIT64 = 6


class XferMode(IntEnum):
    REG = 0  # register ops: page + full OOB as the array holds it
    DMA = 1  # the controller's page engine, controller ECC per FMC_CFG


class NandError(RuntimeError):
    """The agent refused a CMD_NAND request or the hardware failed it."""


def compose_fmc_cfg(live: int, ecc: EccType) -> int:
    """FMC_CFG for SPI NAND, 2 KiB pages, 64-page blocks and ``ecc``.

    Bits the kernel does not touch (SPI_NAND_SEL, address mode, ...) are
    taken from ``live``, the register as it stands.
    """
    cfg = live & ~(
        FMC_CFG_FLASH_SEL_MASK | FMC_CFG_PAGE_SIZE_MASK
        | FMC_CFG_ECC_TYPE_MASK | FMC_CFG_BLOCK_SIZE_MASK
    )
    return (
        cfg | FMC_CFG_OP_MODE_NORMAL | FMC_CFG_FLASH_SEL_SPI_NAND
        | (int(ecc) << FMC_CFG_ECC_TYPE_SHIFT)
    )


def ecc_type_of(fmc_cfg: int) -> EccType:
    return EccType((fmc_cfg & FMC_CFG_ECC_TYPE_MASK) >> FMC_CFG_ECC_TYPE_SHIFT)


@dataclass(frozen=True)
class NandXfer:
    """How the agent moves one page (agent nand_xfer_t)."""

    mode: XferMode = XferMode.DMA
    fmc_cfg: int = 0      # 0 = leave FMC_CFG as it is
    opcode: int = 0x03    # read: 0x03 std / 0x0B fast / 0x6B quad; program: 0x02 / 0x32
    iftype: int = 0       # 0 std, 1 dual, 2 dio, 3 quad, 4 qio
    dummy: int = 1        # dummy bytes after the column address (reads)

    def pack(self) -> bytes:
        return struct.pack(
            "<IBBBB", self.fmc_cfg, int(self.mode), self.opcode, self.iftype, self.dummy,
        )


def program_xfer(read: NandXfer) -> NandXfer:
    """The program-side twin of a read transfer: same mode and FMC_CFG."""
    quad = read.iftype in (3, 4)
    return NandXfer(
        mode=read.mode, fmc_cfg=read.fmc_cfg,
        opcode=0x32 if quad else 0x02, iftype=3 if quad else 0, dummy=0,
    )


@dataclass(frozen=True)
class NandRecord:
    """What the hardware said about one page op (agent nand_rec_t)."""

    ecc_err: int   # FMC ECC_ERR_NUM0_BUF0: a byte per ECC step, 0xff = uncorrectable
    ondie: int     # chip status, feature 0xC0
    fmc_int: int
    flags: int

    @classmethod
    def unpack(cls, raw: bytes) -> NandRecord:
        ecc_err, ondie, fmc_int, flags, _ = struct.unpack("<IBBBB", raw[:REC_SIZE])
        return cls(ecc_err=ecc_err, ondie=ondie, fmc_int=fmc_int, flags=flags)

    def step_errors(self, steps: int) -> list[int]:
        """Per-ECC-step error counts; 0xff means uncorrectable."""
        return [(self.ecc_err >> (8 * i)) & 0xFF for i in range(steps)]

    def uncorrectable_steps(self, steps: int) -> list[int]:
        return [i for i, n in enumerate(self.step_errors(steps)) if n == 0xFF]

    def corrected_bits(self, steps: int) -> int:
        return sum(n for n in self.step_errors(steps) if n != 0xFF)

    @property
    def ondie_ecc(self) -> int:
        """On-die ECC verdict, status bits 5:4 (0 clean, 1 corrected, 2 failed)."""
        return (self.ondie >> 4) & 0x3

    @property
    def failed(self) -> bool:
        return bool(self.flags & (REC_TIMEOUT | REC_FAIL))


@dataclass(frozen=True)
class NandInfo:
    page_size: int
    oob_size: int
    pages_per_block: int
    blocks: int
    dma_buf: int
    dma_size: int
    stat_buf: int
    stat_size: int
    fmc_cfg: int

    @property
    def stride(self) -> int:
        return self.page_size + self.oob_size

    @property
    def pages(self) -> int:
        return self.pages_per_block * self.blocks

    @property
    def ecc_steps(self) -> int:
        """ECC steps per page: the FMC100 BCH engine covers 1 KiB each."""
        return max(1, self.page_size // 1024)


@dataclass
class PageRead:
    page: int
    data: bytes
    oob: bytes
    record: NandRecord


@dataclass
class ScanResult:
    start: int
    records: list[NandRecord] = field(default_factory=list)


def kernel_oob(oob_size: int) -> bytes:
    """The OOB a Linux hifmc100 write sends for a page with no OOB data.

    All 0xff except the empty-page mark: hifmc100_send_cmd_write() zeroes
    the two bytes at oobfree[0].offset + 28 (= 30 with the default
    layout) before every program, so the controller can later tell a
    written page from an erased one.
    """
    oob = bytearray(b"\xff" * oob_size)
    oob[30:32] = b"\x00\x00"
    return bytes(oob)


class NandStudy:
    """CMD_NAND on top of a connected FlashAgentClient."""

    def __init__(self, client: FlashAgentClient) -> None:
        self._client = client
        self._info: NandInfo | None = None
        self.last_status = NAND_ST_OK

    async def _call(self, op: int, args: bytes = b"", timeout: float = 5.0) -> bytes:
        transport = self._client._transport
        self._client._clear_rx_buffers()
        await send_packet(transport, CMD_NAND, bytes([op]) + args)
        deadline = time.monotonic() + timeout
        in_stale_stream = False
        while True:
            try:
                cmd, data = await recv_response(
                    transport, timeout=max(0.1, deadline - time.monotonic()),
                )
            except TransportTimeout:
                raise NandError(f"no answer to CMD_NAND op 0x{op:02x} within {timeout:.0f}s")
            # The tail of a read the host gave up on: the agent finishes the
            # stream regardless, then closes it with an ACK.  An ACK with no
            # data before it is a real answer (an agent without CMD_NAND).
            if cmd == RSP_DATA:
                in_stale_stream = True
                continue
            if cmd == RSP_ACK and in_stale_stream:
                in_stale_stream = False
                continue
            break
        if cmd != RSP_NAND or len(data) < 2 or data[0] != op:
            raise NandError(
                f"agent did not answer CMD_NAND op 0x{op:02x} "
                f"(cmd=0x{cmd:02x}, {len(data)} bytes); does it advertise CAP_NAND?"
            )
        status = data[1]
        if status == NAND_ST_BADARG:
            raise NandError(f"agent rejected CMD_NAND op 0x{op:02x}: bad argument")
        if status == NAND_ST_NOT_NAND:
            raise NandError("agent found no SPI NAND on this board")
        if status not in (NAND_ST_OK, NAND_ST_IO):
            raise NandError(f"CMD_NAND op 0x{op:02x}: unknown status 0x{status:02x}")
        self.last_status = status
        return data[2:]

    async def resync(self, quiet: float = 1.0, limit: float = 60.0) -> None:
        """Drop whatever the agent is still sending until the line is quiet.

        After the host abandons a read (a sequence gap, say), the agent
        still streams the rest of it; this waits that out.
        """
        transport = self._client._transport
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                await recv_packet(transport, quiet)
            except (TransportTimeout, TransportError):
                break
        self._client._clear_rx_buffers()

    async def info(self) -> NandInfo:
        raw = await self._call(NAND_OP_INFO)
        page, oob, ppb, blocks, dma, dma_size, stat, stat_size, cfg = struct.unpack(
            "<HHHHIIIII", raw[:28]
        )
        self._info = NandInfo(page, oob, ppb, blocks, dma, dma_size, stat, stat_size, cfg)
        return self._info

    async def _geometry(self) -> NandInfo:
        return self._info or await self.info()

    async def feature_get(self, addr: int) -> int:
        return (await self._call(NAND_OP_FEATURE_GET, bytes([addr])))[0]

    async def feature_set(self, addr: int, value: int) -> int:
        """Write a feature register; returns what it reads back."""
        return (await self._call(NAND_OP_FEATURE_SET, bytes([addr, value])))[0]

    async def fmc_reg(self, offset: int, value: int | None = None) -> int:
        """Read (or write, then read) a 32-bit FMC register."""
        args = struct.pack("<BHI", 0 if value is None else 1, offset, value or 0)
        return int(struct.unpack("<I", (await self._call(NAND_OP_FMC_REG, args))[:4])[0])

    async def read_pages(
        self, start: int, count: int, xfer: NandXfer, store: bool = True,
    ) -> tuple[list[NandRecord], bytes]:
        """Read ``count`` pages from ``start``.

        Returns the records of the pages the agent got through (it stops
        at the first timeout) and, with ``store``, their page + OOB bytes
        packed at ``stride``.
        """
        info = await self._geometry()
        args = struct.pack("<II", start, count) + xfer.pack() + bytes([1 if store else 0])
        # Register-mode pages take a few ms each; DMA pages well under one.
        raw = await self._call(NAND_OP_READ_PAGES, args, timeout=10.0 + count * 0.02)
        done = struct.unpack("<I", raw[:4])[0]
        recs_raw = await self._client.read_memory(info.stat_buf, done * REC_SIZE, fast=False)
        records = [
            NandRecord.unpack(recs_raw[i * REC_SIZE:(i + 1) * REC_SIZE]) for i in range(done)
        ]
        data = b""
        if store and done:
            data = await self._client.read_memory(info.dma_buf, done * info.stride)
        return records, data

    async def read_page(self, page: int, xfer: NandXfer) -> PageRead:
        info = await self._geometry()
        records, data = await self.read_pages(page, 1, xfer)
        if not records:
            raise NandError(f"page {page}: no result")
        return PageRead(page, data[:info.page_size], data[info.page_size:info.stride], records[0])

    async def scan(
        self, start: int, count: int, xfer: NandXfer, chunk: int = 4096,
    ) -> ScanResult:
        """ECC survey: read every page, keep only the records."""
        info = await self._geometry()
        chunk = min(chunk, info.stat_size // REC_SIZE)
        result = ScanResult(start=start)
        page = start
        while page < start + count:
            n = min(chunk, start + count - page)
            records, _ = await self.read_pages(page, n, xfer, store=False)
            result.records.extend(records)
            if len(records) < n:
                break
            page += n
        return result

    async def program_page(self, page: int, payload: bytes, xfer: NandXfer) -> NandRecord:
        """Program page + OOB exactly as given (no OOB fix-ups)."""
        info = await self._geometry()
        if len(payload) != info.stride:
            raise ValueError(f"need {info.stride} bytes (page + OOB), got {len(payload)}")
        if not await self._client.write_memory(info.dma_buf, payload):
            raise NandError("could not stage the page in agent RAM")
        args = struct.pack("<I", page) + xfer.pack() + struct.pack("<I", 0)
        raw = await self._call(NAND_OP_PROGRAM_PAGE, args, timeout=10.0)
        return NandRecord.unpack(raw[:REC_SIZE])

    async def erase_block(self, page: int) -> NandRecord:
        raw = await self._call(NAND_OP_ERASE_BLOCK, struct.pack("<I", page), timeout=10.0)
        return NandRecord.unpack(raw[:REC_SIZE])
