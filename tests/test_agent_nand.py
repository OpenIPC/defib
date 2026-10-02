"""Tests for the SPI NAND study ops (CMD_NAND) — host side.

FakeNandAgent answers CMD_NAND the way agent/main.c handle_nand() does,
over a simulated chip, and keeps the agent's RAM buffers in a dict so the
CMD_READ / CMD_WRITE half of the protocol can be faked at the client.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from defib.agent import cobs
from defib.agent.nand import (
    FEATURE_CONFIG,
    REC_FAIL,
    EccType,
    NandError,
    NandRecord,
    NandStudy,
    NandXfer,
    XferMode,
    compose_fmc_cfg,
    ecc_type_of,
    kernel_oob,
    program_xfer,
)
from defib.agent.protocol import CMD_NAND, RSP_NAND, build_packet, parse_packet
from defib.transport.mock import MockTransport

PAGE, OOB, PPB, BLOCKS = 2048, 64, 64, 4
STRIDE = PAGE + OOB
DMA_BUF, DMA_SIZE = 0x40E00000, 0x100000
STAT_BUF, STAT_SIZE = 0x42000000, 0x80000


class FakeNandAgent(MockTransport):
    def __init__(self) -> None:
        super().__init__(flush_clears_buffer=False)
        self.pages: dict[int, bytes] = {}
        self.records: dict[int, NandRecord] = {}  # page -> what a read reports
        self.features = {0xA0: 0x00, 0xB0: 0x18, 0xC0: 0x00}
        self.fmc = {0x00: 0x1821}
        self.ram: dict[int, bytes] = {}
        self.calls: list[tuple[int, bytes]] = []
        self.status_override: int | None = None
        self.timeout_at: int | None = None
        self.not_nand = False

    async def write(self, data: bytes) -> None:
        await super().write(data)
        for frame in data.split(b"\x00"):
            if frame:
                cmd, payload = parse_packet(frame)
                assert cmd == CMD_NAND
                self._handle(payload)

    def _reply(self, op: int, status: int, payload: bytes = b"") -> None:
        if self.status_override is not None:
            status = self.status_override
        self.enqueue_rx(build_packet(RSP_NAND, bytes([op, status]) + payload))

    def _handle(self, payload: bytes) -> None:
        op, arg = payload[0], payload[1:]
        self.calls.append((op, arg))
        if op == 0x06:
            write, off, value = struct.unpack("<BHI", arg[:7])
            if write:
                self.fmc[off] = value
            self._reply(op, 0, struct.pack("<I", self.fmc.get(off, 0)))
            return
        if op == 0x00:
            body = struct.pack("<HHHHIIIII", PAGE, OOB, PPB, BLOCKS, DMA_BUF, DMA_SIZE,
                               STAT_BUF, STAT_SIZE, self.fmc[0])
            self._reply(op, 2 if self.not_nand else 0, body)
            return
        if self.not_nand:
            self._reply(op, 2)
            return
        if op == 0x01:
            self._reply(op, 0, bytes([self.features.get(arg[0], 0)]))
        elif op == 0x02:
            self.features[arg[0]] = arg[1]
            self._reply(op, 0, bytes([arg[1]]))
        elif op == 0x03:
            start, count = struct.unpack("<II", arg[:8])
            store = arg[16]
            recs = bytearray()
            data = bytearray()
            done = 0
            for p in range(start, start + count):
                rec = self.records.get(p, NandRecord(0, 0, 1, 0))
                if self.timeout_at == p:
                    rec = NandRecord(0, 0xFF, 0, 1)
                recs += struct.pack("<IBBBB", rec.ecc_err, rec.ondie, rec.fmc_int, rec.flags, 0)
                data += self.pages.get(p, b"\xff" * STRIDE)
                done += 1
                if self.timeout_at == p:
                    break
            self.ram[STAT_BUF] = bytes(recs)
            if store:
                self.ram[DMA_BUF] = bytes(data)
            self._reply(op, 0 if done == count else 3, struct.pack("<I", done))
        elif op == 0x04:
            page = struct.unpack("<I", arg[:4])[0]
            src_off = struct.unpack("<I", arg[12:16])[0]
            self.pages[page] = self.ram[DMA_BUF + src_off][:STRIDE]
            self._reply(op, 0, struct.pack("<IBBBB", 0, 0, 1, 0, 0))
        elif op == 0x05:
            page = struct.unpack("<I", arg[:4])[0]
            first = page - page % PPB
            for p in range(first, first + PPB):
                self.pages.pop(p, None)
            self._reply(op, 0, struct.pack("<IBBBB", 0, 0, 1, 0, 0))
        else:
            self._reply(op, 1)


class FakeClient:
    """The bits of FlashAgentClient NandStudy uses, over FakeNandAgent RAM."""

    CAP_NAND = 1 << 8

    def __init__(self, agent: FakeNandAgent) -> None:
        self._transport = agent
        self.agent = agent

    def _clear_rx_buffers(self) -> None:
        pass

    async def _restore_baud(self) -> None:
        pass

    async def read_memory(self, addr: int, size: int, fast: bool = True) -> bytes:
        return self.agent.ram[addr][:size]

    async def write_memory(self, addr: int, data: bytes) -> bool:
        self.agent.ram[addr] = data
        return True


@pytest.fixture
def agent() -> FakeNandAgent:
    return FakeNandAgent()


@pytest.fixture
def study(agent: FakeNandAgent) -> NandStudy:
    return NandStudy(FakeClient(agent))  # type: ignore[arg-type]


# --- pure helpers -------------------------------------------------------------

def test_compose_fmc_cfg_keeps_unrelated_bits() -> None:
    live = 0x1821  # bootrom/agent default: NOR select, ECC 8
    cfg = compose_fmc_cfg(live, EccType.BIT24)
    assert cfg & 0x1 == 1                     # normal mode
    assert (cfg >> 1) & 0x3 == 1              # SPI NAND
    assert ecc_type_of(cfg) == EccType.BIT24
    assert (cfg >> 3) & 0x3 == 0 and (cfg >> 8) & 0x3 == 0  # 2K page, 64-page block
    assert cfg & 0x1800 == 0x1800             # SPI_NAND_SEL untouched
    assert ecc_type_of(compose_fmc_cfg(cfg, EccType.NONE)) == EccType.NONE


def test_xfer_packing() -> None:
    x = NandXfer(mode=XferMode.DMA, fmc_cfg=0x11823, opcode=0x6B, iftype=3, dummy=1)
    assert x.pack() == struct.pack("<IBBBB", 0x11823, 1, 0x6B, 3, 1)
    w = program_xfer(x)
    assert (w.opcode, w.iftype, w.fmc_cfg, w.mode) == (0x32, 3, 0x11823, XferMode.DMA)
    assert program_xfer(NandXfer()).opcode == 0x02


def test_record_decoding() -> None:
    rec = NandRecord.unpack(struct.pack("<IBBBB", 0x0000FF00, 0x20, 0x01, 0, 0))
    assert rec.step_errors(2) == [0, 0xFF]
    assert rec.uncorrectable_steps(2) == [1]          # the #2519 signature
    assert rec.corrected_bits(2) == 0
    assert rec.ondie_ecc == 2
    assert not rec.failed
    assert NandRecord(0x0302, 0, 0, 0).corrected_bits(2) == 5
    assert NandRecord(0, 0, 0, REC_FAIL).failed


def test_kernel_oob_marks_page_written() -> None:
    oob = kernel_oob(64)
    assert len(oob) == 64
    assert oob[30:32] == b"\x00\x00"
    assert oob[:30] == b"\xff" * 30 and oob[32:] == b"\xff" * 32


# --- NandStudy against the fake agent -----------------------------------------

async def test_info(study: NandStudy) -> None:
    info = await study.info()
    assert (info.page_size, info.oob_size, info.pages_per_block, info.blocks) == (PAGE, OOB, PPB, BLOCKS)
    assert info.stride == STRIDE and info.pages == PPB * BLOCKS and info.ecc_steps == 2
    assert (info.dma_buf, info.stat_buf, info.fmc_cfg) == (DMA_BUF, STAT_BUF, 0x1821)


async def test_read_page_splits_data_and_oob(study: NandStudy, agent: FakeNandAgent) -> None:
    agent.pages[7] = bytes(range(256)) * 8 + b"\xab" * OOB
    agent.records[7] = NandRecord(0x0000FF00, 0x20, 1, 0)
    r = await study.read_page(7, NandXfer())
    assert r.data == bytes(range(256)) * 8
    assert r.oob == b"\xab" * OOB
    assert r.record.uncorrectable_steps(2) == [1]


async def test_scan_chunks_and_keeps_order(study: NandStudy, agent: FakeNandAgent) -> None:
    agent.records[130] = NandRecord(0xFF, 0, 1, 0)
    result = await study.scan(0, 200, NandXfer(), chunk=64)
    assert len(result.records) == 200
    assert [i for i, r in enumerate(result.records) if r.uncorrectable_steps(2)] == [130]
    reads = [struct.unpack("<II", a[:8]) for op, a in agent.calls if op == 0x03]
    assert reads == [(0, 64), (64, 64), (128, 64), (192, 8)]
    assert all(a[16] == 0 for op, a in agent.calls if op == 0x03)  # scan discards data


async def test_scan_stops_at_timeout(study: NandStudy, agent: FakeNandAgent) -> None:
    agent.timeout_at = 70
    result = await study.scan(0, 200, NandXfer(), chunk=64)
    assert len(result.records) == 71
    assert result.records[-1].failed


async def test_program_page_stages_exact_bytes(study: NandStudy, agent: FakeNandAgent) -> None:
    payload = b"\x5a" * PAGE + kernel_oob(OOB)
    rec = await study.program_page(9, payload, program_xfer(NandXfer()))
    assert not rec.failed
    assert agent.pages[9] == payload
    with pytest.raises(ValueError, match="2112"):
        await study.program_page(9, b"\x00" * PAGE, NandXfer())


async def test_erase_block(study: NandStudy, agent: FakeNandAgent) -> None:
    agent.pages[65] = b"\x00" * STRIDE
    await study.erase_block(64)
    assert 65 not in agent.pages


async def test_feature_and_fmc_reg(study: NandStudy, agent: FakeNandAgent) -> None:
    assert await study.feature_get(FEATURE_CONFIG) == 0x18
    assert await study.feature_set(FEATURE_CONFIG, 0x08) == 0x08
    assert agent.features[0xB0] == 0x08
    assert await study.fmc_reg(0x00) == 0x1821
    assert await study.fmc_reg(0x00, 0x11823) == 0x11823


async def test_errors_raise(study: NandStudy, agent: FakeNandAgent) -> None:
    agent.status_override = 1
    with pytest.raises(NandError, match="bad argument"):
        await study.feature_get(0xB0)
    agent.status_override = None
    agent.not_nand = True
    with pytest.raises(NandError, match="no SPI NAND"):
        await study.feature_get(0xB0)


async def test_unexpected_answer_raises(agent: FakeNandAgent) -> None:
    class Silent(FakeNandAgent):
        def _handle(self, payload: bytes) -> None:
            self.enqueue_rx(build_packet(0x83, b"\x01"))  # plain ACK: old agent

    study = NandStudy(FakeClient(Silent()))  # type: ignore[arg-type]
    with pytest.raises(NandError, match="CAP_NAND"):
        await study.info()


async def test_stale_read_tail_is_skipped(agent: FakeNandAgent) -> None:
    """A read the host abandoned keeps streaming; the next call must not
    take its frames for the answer."""
    class Stale(FakeNandAgent):
        first = True

        def _handle(self, payload: bytes) -> None:
            if self.first:
                self.first = False
                self.enqueue_rx(build_packet(0x82, b"\x05\x00" + b"\xaa" * 64))
                self.enqueue_rx(build_packet(0x83, b"\x00"))
                self.enqueue_rx(build_packet(0x81, b"\x00" * 28))  # late INFO answer
            FakeNandAgent._handle(self, payload)

    study = NandStudy(FakeClient(Stale()))  # type: ignore[arg-type]
    assert (await study.info()).page_size == PAGE


# --- CLI ------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, agent: FakeNandAgent) -> CliRunner:
    import asyncio

    from defib.cli import nand as nand_cli

    def fake_run(port: str, body: Any) -> Any:
        return asyncio.run(body(NandStudy(FakeClient(agent))))  # type: ignore[arg-type]

    monkeypatch.setattr(nand_cli, "_run", fake_run)
    return CliRunner()


def _app() -> Any:
    from defib.cli.app import app
    return app


def test_cli_ecc_scan_lists_uncorrectable(
    cli: CliRunner, agent: FakeNandAgent, tmp_path: Path,
) -> None:
    agent.records[65] = NandRecord(0x0000FF00, 0, 1, 0)
    agent.records[66] = NandRecord(0x00000003, 0, 1, 0)
    out = tmp_path / "scan.json"
    res = cli.invoke(_app(), ["agent", "nand", "ecc-scan", "--count", "128",
                              "--json", str(out)])
    assert res.exit_code == 0, res.output
    assert "uncorrectable: 1   corrected: 1" in res.output
    assert "page     65 (block    1 page  1)" in res.output
    assert json.loads(out.read_text())["records"][65][0] == 0xFF00
    # Default transfer: controller page engine, 8-bit ECC on a SPI NAND config.
    xfer = NandXfer(fmc_cfg=compose_fmc_cfg(0x1821, EccType.BIT8))
    assert any(a[8:16] == xfer.pack() for op, a in agent.calls if op == 0x03)


def test_cli_dump_switches_ondie_ecc_off_and_back(
    cli: CliRunner, agent: FakeNandAgent, tmp_path: Path,
) -> None:
    agent.pages[1] = b"\x11" * STRIDE
    out = tmp_path / "nand.bin"
    res = cli.invoke(_app(), ["agent", "nand", "dump", "-o", str(out), "--count", "3"])
    assert res.exit_code == 0, res.output
    assert out.read_bytes() == b"\xff" * STRIDE + b"\x11" * STRIDE + b"\xff" * STRIDE
    sidecar = json.loads((tmp_path / "nand.bin.json").read_text())
    assert sidecar["pages"] == 3 and sidecar["mode"] == "reg"
    assert sidecar["features"]["config_b0"] == 0x18
    sets = [a for op, a in agent.calls if op == 0x02]
    assert sets == [bytes([0xB0, 0x08]), bytes([0xB0, 0x18])]  # off for the dump, then back
    # Raw dump: register reads, FMC_CFG untouched.
    assert all(a[8:16] == NandXfer(mode=XferMode.REG).pack()
               for op, a in agent.calls if op == 0x03)


def test_cli_dump_incomplete_is_an_error(
    cli: CliRunner, agent: FakeNandAgent, tmp_path: Path,
) -> None:
    agent.timeout_at = 2
    out = tmp_path / "nand.bin"
    res = cli.invoke(_app(), ["agent", "nand", "dump", "-o", str(out), "--count", "5"])
    assert res.exit_code == 1
    assert "INCOMPLETE" in res.output
    assert out.read_bytes() == b"\xff" * STRIDE * 2       # the failed page is not kept
    sidecar = json.loads((tmp_path / "nand.bin.json").read_text())
    assert sidecar["complete"] is False and sidecar["failed_page"] == 2
    assert sidecar["pages"] == 2


def test_cli_dump_refuses_when_ondie_ecc_stays_on(
    cli: CliRunner, agent: FakeNandAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = FakeNandAgent._handle

    def sticky(self: FakeNandAgent, payload: bytes) -> None:
        if payload[0] == 0x02:   # FEATURE_SET ignored by the chip
            self.calls.append((0x02, payload[1:]))
            self._reply(0x02, 0, bytes([self.features[payload[1]]]))
            return
        original(self, payload)

    monkeypatch.setattr(FakeNandAgent, "_handle", sticky)
    out = tmp_path / "nand.bin"
    res = cli.invoke(_app(), ["agent", "nand", "dump", "-o", str(out), "--count", "2"])
    assert res.exit_code == 1
    assert "kept its on-die ECC on" in res.output
    assert not any(op == 0x03 for op, _ in agent.calls)   # nothing was read


def test_cli_write_page_kernel_oob(cli: CliRunner, agent: FakeNandAgent, tmp_path: Path) -> None:
    data = tmp_path / "page.bin"
    data.write_bytes(b"\x42" * PAGE)
    res = cli.invoke(_app(), ["agent", "nand", "write-page", "5", "-i", str(data),
                              "--kernel-oob"])
    assert res.exit_code == 0, res.output
    assert agent.pages[5] == b"\x42" * PAGE + kernel_oob(OOB)
    res = cli.invoke(_app(), ["agent", "nand", "write-page", "5", "-i", str(data)])
    assert res.exit_code != 0


def test_cobs_roundtrip_sanity() -> None:
    """The fake decodes real frames, not a shortcut."""
    pkt = build_packet(CMD_NAND, b"\x00")
    assert cobs.decode(pkt[:-1])[0] == CMD_NAND
