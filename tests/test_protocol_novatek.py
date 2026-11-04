"""Tests for the Novatek UART boot protocol.

FakeDevice follows the two ends as they are known: the write loader from the
SDK's uart_upgrade.c, which answers a command before it takes the data; the
ROM from FwUpgrade.exe, which only shows that the host sends command and data
together and reads the answer after, so the fake answers last.
"""

import asyncio
import io
import struct
import tarfile
from collections import deque

import pytest

from defib.firmware import asset_name, firmware_url
from defib.protocol.base import ProtocolError
from defib.protocol.novatek import (
    DIE_SRAM,
    NOVATEK_SOCS,
    OP_ADDR_WRITE,
    OP_GET_TMP_ADDR,
    OP_IS_NVT,
    OP_MEM_WRITE,
    OP_OPEN_DEVICE,
    OP_UBOOT,
    OP_WAIT_DONE,
    OP_WRLOADER,
    Novatek,
    build_cdb,
    defuse_loader,
    load_recovery_set,
)
from defib.protocol.registry import find_protocol
from defib.recovery.events import Stage
from defib.recovery.session import RecoverySession
from defib.transport.base import Transport, TransportTimeout

NA51089 = 0x70210000
SRAM = 0xF07C0000
TMP = 0x02000000
NOISE = b"dma1 ini_ver 0x00000001\r\nLoader Start ...\r\n"


def loader_image(base: int = SRAM, size: int = 0x8000) -> bytes:
    return struct.pack("<II", base + 0x28, base).ljust(size, b"\x5a")


def archive(sets: dict[str, dict[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for folder, files in sets.items():
            for name, data in files.items():
                info = tarfile.TarInfo(f"./{folder}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class FakeDevice(Transport):
    """The ROM, until WAIT_DONE, then the write loader, on one byte stream."""

    def __init__(self, word: int = NA51089, boot_after: int = 0, status: int = 0) -> None:
        self.word = word
        self.boot_after = boot_after    # IS_NVTs unanswered: not at UART yet
        self.status = status            # what UBOOT reports
        self.rx = bytearray()
        self.tx: deque[int] = deque()
        self.in_rom = True
        self.ops: list[int] = []
        self.sram: dict[int, bytes] = {}
        self.dram: dict[int, bytes] = {}
        self.booted = False
        self.bad_sums = 0

    def _answer(self, cdb: bytes, status: int = 0, data: bytes = b"") -> None:
        reply = bytearray(cdb)
        reply[13] = status
        self.tx.extend(reply + data)

    def _pump(self) -> None:
        while len(self.rx) >= 16:
            cdb = bytes(self.rx[:16])
            words = struct.unpack("<8H", cdb)
            if sum(w + i for i, w in enumerate(words)) & 0xFFFF:
                self.bad_sums += 1
                del self.rx[:16]
                continue
            op = cdb[1]
            addr, size = struct.unpack_from("<II", cdb, 2)
            if op in (OP_WRLOADER, OP_MEM_WRITE) and len(self.rx) < 16 + size:
                return
            del self.rx[:16]
            self.ops.append(op)
            if op == OP_IS_NVT:
                self._answer(cdb, data=struct.pack("<I", self.word))
            elif op == OP_OPEN_DEVICE:
                self._answer(cdb)
            elif op == OP_WRLOADER:
                assert self.in_rom
                data = bytes(self.rx[:size])
                # The ROM decodes every 16 bytes of the payload as well
                for off in range(0, size, 16):
                    word = int.from_bytes(data[off:off + 4], "little")
                    assert word not in (0x81C0, 0x01C0, 0x82C0), hex(off)
                    assert word & 0xFFFF not in (0x83C0, 0x03C0, 0x02C0), hex(off)
                self.sram[addr] = data
                del self.rx[:size]
                self._answer(cdb)
            elif op == OP_ADDR_WRITE:
                assert self.in_rom
                (base, image), = self.sram.items()
                at = addr - base
                self.sram[base] = image[:at] + size.to_bytes(4, "little") + image[at + 4:]
                self._answer(cdb)
            elif op == OP_WAIT_DONE and self.in_rom:
                self._answer(cdb)
                self.in_rom = False
                self.tx.extend(NOISE)
            elif op == OP_WAIT_DONE:
                self._answer(cdb)
                self.booted = True
            elif op == OP_GET_TMP_ADDR:
                self._answer(cdb, data=struct.pack("<I", TMP))
            elif op == OP_MEM_WRITE:
                assert not self.in_rom
                self.tx.extend(NOISE[:5])
                self._answer(cdb)
                self.dram[addr] = bytes(self.rx[:size])
                del self.rx[:size]
            elif op == OP_UBOOT:
                self._answer(cdb, data=struct.pack("<i", self.status))
            else:
                raise AssertionError(f"unexpected op {op:#x}")

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        self._pump()
        if not self.tx:
            await asyncio.sleep(0)
            raise TransportTimeout("nothing")
        n = min(size, len(self.tx))
        return bytes(self.tx.popleft() for _ in range(n))

    async def write(self, data: bytes) -> None:
        if self.boot_after:     # not at UART yet: nothing is read
            self.boot_after -= len(data) == 16
            return
        self.rx += data

    async def flush_input(self) -> None:
        self.tx.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self.tx)

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def no_ddr_wait(monkeypatch):
    monkeypatch.setattr("defib.protocol.novatek.LOADER_START_DELAY", 0)


def bundle(loader: bytes | None = None, pack: bytes = b"P" * 10000) -> bytes:
    return archive({
        "recovery-nt98562-nor": {
            "wrloader.bin": loader or loader_image(),
            "FW98562T.BIN": pack,
            "loader.bin": b"eth",
        },
        "recovery-nt98562-nand": {
            "wrloader.bin": b"nand" * 2,
            "FW98562T.BIN": b"nandpack",
        },
        "recovery-nt98525-nor": {"FW98525T.BIN": b"x", "loader.bin": b"eth"},
    })


class TestCommand:
    def test_format(self):
        cdb = build_cdb(0x04, 0x02000000, 0x1000)
        assert len(cdb) == 16
        assert cdb[:2] == b"\xc0\x04"
        assert struct.unpack_from("<II", cdb, 2) == (0x02000000, 0x1000)

    def test_the_loaders_sum_comes_to_zero(self):
        # uart_upgrade.c's calc_sum(): the halves of the four words, plus 28
        cdb = build_cdb(0x81)
        assert (sum(struct.unpack("<8H", cdb)) + 28) & 0xFFFF == 0


class TestMatching:
    def test_chips_find_the_protocol(self):
        for soc in NOVATEK_SOCS:
            assert find_protocol(soc) is Novatek

    def test_nt9852x_has_no_uart_boot(self):
        for soc in ("nt98520", "nt98525", "nt98528"):
            assert not Novatek.matches(soc)

    def test_one_archive_for_every_chip(self):
        assert asset_name("nt98562") == "novatek-recovery.tar.gz"
        assert asset_name("nt98323", "nand") == "novatek-recovery.tar.gz"
        assert firmware_url("nt98533") == (
            "https://github.com/OpenIPC/u-boot/releases/download/novatek/"
            "novatek-recovery.tar.gz"
        )


class TestDefuse:
    def test_pads_and_patches(self):
        loader = bytes(16) + b"\xc0\x81\x00\x00" + b"\x11" * 12 + b"\x22" * 5
        image, patches = defuse_loader(loader)
        assert len(image) == 48
        assert image[16:32] == bytes(16)
        assert patches[0] == (16, 0x81C0)
        assert patches[1] == (20, 0x11111111)

    def test_leaves_a_clean_loader_alone(self):
        image, patches = defuse_loader(loader_image())
        assert image == loader_image() and not patches


class TestArchive:
    def test_picks_the_flash(self):
        loader, pack = load_recovery_set(bundle(), "nt98562", "nand")
        assert (loader, pack) == (b"nand" * 2, b"nandpack")

    def test_no_uart_set(self):
        with pytest.raises(ProtocolError, match="no UART set"):
            load_recovery_set(bundle(), "nt98525")

    def test_not_an_archive(self):
        with pytest.raises(ProtocolError, match="not a recovery archive"):
            load_recovery_set(b"u-boot", "nt98562")

    def test_a_truncated_archive(self):
        data = bundle()
        with pytest.raises(ProtocolError, match="not a recovery archive"):
            load_recovery_set(data[:len(data) // 2], "nt98562")

    def test_an_empty_pack(self):
        with pytest.raises(ProtocolError, match="empty file"):
            load_recovery_set(bundle(pack=b""), "nt98562")


@pytest.mark.asyncio
class TestRecovery:
    def protocol(self) -> Novatek:
        proto = Novatek()
        proto.set_target("nt98562")
        return proto

    async def test_both_stages(self):
        loader, pack = loader_image(), bytes(range(256)) * 40
        dev, proto = FakeDevice(boot_after=3), self.protocol()
        assert (await proto.handshake(dev)).chip_id == NA51089
        misaligned = dev.bad_sums
        result = await proto.send_firmware(dev, bundle(loader, pack))
        assert result.success, result.error
        assert result.stages_completed[-1] == Stage.COMPLETE
        assert dev.bad_sums == misaligned
        assert dev.sram == {SRAM: loader}
        assert dev.ops[:4] == [OP_IS_NVT, OP_OPEN_DEVICE, OP_WRLOADER, OP_WAIT_DONE]
        assert dev.ops[-2:] == [OP_UBOOT, OP_WAIT_DONE]
        assert b"".join(dev.dram[a] for a in sorted(dev.dram)) == pack
        assert min(dev.dram) == TMP
        assert dev.booted

    async def test_a_running_loader_skips_the_rom(self):
        dev, proto = FakeDevice(), self.protocol()
        dev.in_rom = False
        proto.loader_running = True
        await proto.handshake(dev)
        assert (await proto.send_firmware(dev, bundle())).success
        assert OP_WRLOADER not in dev.ops

    async def test_another_die_is_refused(self):
        dev, proto = FakeDevice(word=0x81200000), self.protocol()
        result = await proto.handshake(dev)
        assert not result.success
        assert "bring the DDR up" in result.message
        assert OP_OPEN_DEVICE not in dev.ops

    async def test_a_stray_byte_is_walked_past(self):
        dev, proto = FakeDevice(), self.protocol()
        dev.rx += b"\x55" * 5     # line noise the ROM took as a command's start
        assert (await proto.handshake(dev)).chip_id == NA51089

    async def test_an_unknown_die_is_refused(self):
        dev, proto = FakeDevice(word=0x12345678), self.protocol()
        result = await proto.handshake(dev)
        assert not result.success
        assert OP_OPEN_DEVICE not in dev.ops

    async def test_a_64k_loader_is_one_command(self):
        loader = loader_image(size=0x10000)
        dev, proto = FakeDevice(), self.protocol()
        await proto.handshake(dev)
        result = await proto.send_firmware(dev, bundle(loader))
        assert result.success, result.error
        assert dev.ops.count(OP_WRLOADER) == 1
        assert dev.sram == {SRAM: loader}

    async def test_a_loader_chunk_the_rom_would_run_is_written_back(self):
        loader = bytearray(loader_image())
        loader[0x910:0x914] = b"\xc0\x02\x9f\xe5"
        loader = bytes(loader)
        dev, proto = FakeDevice(), self.protocol()
        await proto.handshake(dev)
        result = await proto.send_firmware(dev, bundle(loader))
        assert result.success, result.error
        assert dev.sram == {SRAM: loader}
        assert dev.ops.count(OP_ADDR_WRITE) == 4

    async def test_a_loader_past_the_rom_limit_is_refused(self):
        dev, proto = FakeDevice(), self.protocol()
        await proto.handshake(dev)
        result = await proto.send_firmware(dev, bundle(loader_image(size=0x10001)))
        assert not result.success
        assert "more than the ROM" in result.error
        assert not dev.sram

    async def test_a_loader_for_another_sram_is_refused(self):
        dev, proto = FakeDevice(), self.protocol()
        await proto.handshake(dev)
        result = await proto.send_firmware(dev, bundle(loader_image(0xFE080000)))
        assert not result.success
        assert "linked at" in result.error
        assert not dev.sram

    async def test_the_loader_refusing_the_pack(self):
        dev, proto = FakeDevice(status=-1), self.protocol()
        await proto.handshake(dev)
        result = await proto.send_firmware(dev, bundle())
        assert not result.success
        assert "refuses the pack" in result.error
        assert not dev.booted

    async def test_silence(self, monkeypatch):
        monkeypatch.setattr("defib.protocol.novatek.HANDSHAKE_TIMEOUT", 0.05)
        monkeypatch.setattr("defib.protocol.novatek.HANDSHAKE_INTERVAL", 0.01)
        result = await self.protocol().handshake(FakeDevice(boot_after=10**6))
        assert not result.success

    async def test_the_session_names_the_set(self, tmp_path):
        path = tmp_path / "novatek-recovery.tar.gz"
        path.write_bytes(bundle())
        dev = FakeDevice()
        session = RecoverySession(chip="nt98562", firmware_path=str(path))
        result = await session.run(dev)
        assert result.success, result.error
        assert dev.booted


def test_every_die_has_its_sram():
    from defib.protocol.novatek import NOVATEK_DIES

    assert set(NOVATEK_DIES.values()) == set(DIE_SRAM)


@pytest.mark.asyncio
async def test_a_silent_board_fails_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr("defib.protocol.novatek.HANDSHAKE_TIMEOUT", 0.05)
    monkeypatch.setattr("defib.protocol.novatek.HANDSHAKE_INTERVAL", 0.01)
    path = tmp_path / "novatek-recovery.tar.gz"
    path.write_bytes(bundle())
    session = RecoverySession(chip="nt98562", firmware_path=str(path))
    result = await session.run(FakeDevice(boot_after=10**6))
    assert not result.success
    assert "No answer" in (result.error or "")

