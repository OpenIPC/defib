"""GM8126 USB updater: framing, write order, refusals, and the commands.

No hardware was available: the fake below implements the device side as
fsm.bin does (word-swapped SPI data, D8 erases of one block, page-sized
programs), so it checks defib against that reading of it, not against a chip.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from defib.grainmedia import cli, device, protocol as p
from defib.grainmedia.images import install_segments, restore_segments
from defib.grainmedia.recovery import GrainMediaRecovery, Segment, check_segments
from defib.install.model import InstallRequest
from defib.profiles.loader import load_profile, usb_family
from defib.profiles.schema import SoCProfile

MIB = 0x100000


class FakeUpdater:
    """The updater, over a NOR chip of ``size`` bytes held in flash order."""

    def __init__(self, size: int = 8 * MIB, fill: bytes = b"") -> None:
        self.size = size
        self.flash = bytearray(fill.ljust(size, b"\xff"))
        self.flash_id = p.FLASH_ID_SPI
        self.inquiry = p.INQUIRY_PREFIX[:8] + b"Grain   GM8126          1.11"
        self.can_read = True
        self.locked = False
        self.log: list[tuple[str, int]] = []
        self.fail_after: int | None = None

    def open(self) -> None: ...
    def close(self) -> None: ...

    def command(self, command: p.Command, data_out: bytes = b"") -> bytes:
        cdb = command.cdb
        if cdb[0] == 0x12:
            return self.inquiry
        op = cdb[1]
        if op == 0x1E:
            return self.flash_id
        if op == 0x25:
            assert cdb[2:5] == bytes([4, 8, 16])
            self.log.append(("geometry", cdb[5]))
            return b""
        if op in (0x21, 0x23):
            if self.fail_after is not None:
                self.fail_after -= 1
                if self.fail_after < 0:
                    raise device.GrainMediaUsbError("cable pulled")
        (arg,) = struct.unpack_from(">I", cdb, 2)
        if op == 0x22:
            if not self.can_read:
                raise device.GrainMediaUsbError("command f0 22 was refused (status 1)")
            count = struct.unpack_from(">I", cdb, 6)[0] * 256
            at = arg * 256 % self.size
            data = bytes((self.flash[at:] + self.flash)[:count] if at + count > self.size
                         else self.flash[at:at + count])
            return p.swap_words(data)
        if op == 0x23:
            assert struct.unpack_from(">H", cdb, 6) == (1,)
            self.log.append(("erase", arg))
            at = arg % self.size
            if not self.locked:
                self.flash[at:at + 0x10000] = b"\xff" * 0x10000
            return b""
        if op == 0x21:
            assert len(data_out) == 256
            self.log.append(("program", arg * 256))
            at = arg * 256 % self.size
            page = p.swap_words(data_out)
            self.flash[at:at + 256] = bytes(
                a & b for a, b in zip(self.flash[at:at + 256], page)
            )
            return b""
        raise AssertionError(f"unexpected command {cdb.hex()}")


def boot_image(length: int = 0x38930) -> bytes:
    header = bytearray(256)
    header[:8] = p.HEADER_MAGIC
    struct.pack_into("<III", header, 0x34, 8, 12, 24)
    header[0xFE:] = p.HEADER_END
    body = bytearray((i * 7 + i // 251) & 0xFF for i in range(length))
    body[:256] = p.swap_words(bytes(header))
    body[0x1000:0x1004] = struct.pack(">I", p.IMAGE_MAGIC)
    return bytes(body)


def chip_image(size: int = 8 * MIB) -> bytes:
    """A whole chip with every block different."""
    out = bytearray()
    for block in range(size // 0x10000):
        out += (struct.pack("<I", block * 2654435761 % 2**32) * 64 + bytes(range(256))) * 128
    out[:0x1004] = boot_image()[:0x1004]
    return bytes(out)


def firmware(tmp_path: Path, board: str = "gm8126") -> str:
    path = tmp_path / "fw.tgz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (
            (f"uImage.{board}", b"\x27\x05\x19\x56" + b"k" * 100000),
            (f"rootfs.squashfs.{board}", b"hsqs" + b"r" * 200000),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return str(path)


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeUpdater:
    fake = FakeUpdater(fill=chip_image())

    async def found(*_: object, **__: object) -> object:
        return "GM8126 updater"

    monkeypatch.setattr(cli, "wait_for_device", found)
    monkeypatch.setattr(cli, "GrainMediaDevice", lambda _: fake)
    monkeypatch.chdir(tmp_path)
    return fake


class TestFraming:
    """Byte positions as the device's dispatcher reads them."""

    def test_cbw(self):
        cbw = p.build_cbw(0x11223344, p.read_pages(0x010203, 0x80))
        assert len(cbw) == 31
        assert cbw[:8] == b"USBC\x44\x33\x22\x11"
        assert cbw[8:12] == (0x8000).to_bytes(4, "little")
        assert cbw[12:15] == bytes([0x80, 0, 10])
        assert cbw[15:25] == bytes.fromhex("f0220001020300000080")

    @pytest.mark.parametrize("command,cdb,length,direction", [
        (p.read_flash_id(), "f01e00000000", 5, True),
        (p.set_spi_geometry(23), "f02504081017", 0, False),
        (p.erase_block(0x120000), "f0230012000000010000", 0, False),
        (p.program_page(0x1234), "f0210000123400000000", 256, False),
        (p.inquiry(), "120000002400", 36, True),
    ])
    def test_cdbs(self, command, cdb, length, direction):
        assert command.cdb.hex() == cdb
        assert (command.length, command.direction_in) == (length, direction)

    @pytest.mark.parametrize("address", [0x100, 0x1000000, -0x10000])
    def test_erase_address_is_checked(self, address):
        with pytest.raises(ValueError):
            p.erase_block(address)

    def test_csw(self):
        good = struct.pack("<4sIIB", b"USBS", 7, 3, 1)
        assert p.parse_csw(good, 7) == (3, 1)
        for bad in (good[:12], b"X" + good[1:]):
            with pytest.raises(p.GrainMediaError):
                p.parse_csw(bad, 7)
        with pytest.raises(p.GrainMediaError, match="tag"):
            p.parse_csw(good, 8)

    def test_word_swap(self):
        assert p.swap_words(bytes(range(8))) == bytes([3, 2, 1, 0, 7, 6, 5, 4])
        with pytest.raises(ValueError):
            p.swap_words(b"abc")


class TestBootImage:
    def test_good(self):
        p.validate_boot_image(boot_image())

    def test_vendor_byte_order_is_named(self):
        image = boot_image(0x38930)
        with pytest.raises(p.GrainMediaError, match="byte order"):
            p.validate_boot_image(p.swap_words(image))

    @pytest.mark.parametrize("image,match", [
        (b"", "no GM8126 system header"),
        (b"\xff" * 0x40000, "no GM8126 system header"),
        (boot_image()[:0x2000], "ends inside"),
        (boot_image()[:0x1000] + b"\0" * 0x8000, "nsboot image header"),
    ])
    def test_bad(self, image, match):
        with pytest.raises(p.GrainMediaError, match=match):
            p.validate_boot_image(image)


class TestRecovery:
    async def test_identify(self):
        assert await GrainMediaRecovery(FakeUpdater()).identify() == "GM8126"

    @pytest.mark.parametrize("product", [b"GM8136", b"GM8180"])
    async def test_another_soc_is_refused(self, product):
        fake = FakeUpdater()
        fake.inquiry = fake.inquiry.replace(b"GM8126", product)
        with pytest.raises(p.GrainMediaError, match="not a GM8126"):
            await GrainMediaRecovery(fake).identify()

    async def test_an_ordinary_usb_stick_is_refused(self):
        fake = FakeUpdater()
        fake.inquiry = p.INQUIRY_PREFIX[:8] + b"SanDisk Cruzer          1.11"
        with pytest.raises(p.GrainMediaError, match="identify"):
            await GrainMediaRecovery(fake).identify()

    async def test_nand_is_refused(self):
        fake = FakeUpdater()
        fake.flash_id = bytes.fromhex("ecf1009540")
        with pytest.raises(p.GrainMediaError, match="NAND"):
            await GrainMediaRecovery(fake).identify()

    @pytest.mark.parametrize("size", [4 * MIB, 8 * MIB, 16 * MIB])
    async def test_size_from_the_wrap(self, size):
        assert await GrainMediaRecovery(FakeUpdater(size, chip_image(size))).detect_size() == size

    async def test_an_erased_chip_has_no_size(self):
        assert await GrainMediaRecovery(FakeUpdater()).detect_size() is None

    async def test_read_is_in_flash_order(self):
        image = chip_image()
        got = await GrainMediaRecovery(FakeUpdater(fill=image)).read(0x10100, 0x12300)
        assert got == image[0x10100:0x10100 + 0x12300]

    async def test_header_goes_first_to_erase_and_last_to_program(self):
        fake = FakeUpdater(fill=chip_image())
        before = bytes(fake.flash)
        new = boot_image()
        await GrainMediaRecovery(fake).write(
            [Segment("kernel", 0x50000, b"K" * 70000), Segment("boot", 0, new)], 8 * MIB
        )
        ops = [entry for entry in fake.log if entry[0] != "geometry"]
        assert ops[0] == ("erase", 0)
        assert ops[-1] == ("program", 0)
        assert ops.count(("program", 0)) == 1
        assert fake.flash[:len(new)] == new
        assert fake.flash[0x50000:0x50000 + 70000] == b"K" * 70000
        assert fake.flash[0x40000:0x50000] == before[0x40000:0x50000]
        assert fake.flash[0x70000:] == before[0x70000:]

    async def test_a_cut_anywhere_leaves_no_header(self):
        # 4 erases and 906 pages: the last cut falls on the header itself
        for cut in (1, 5, 300, 900, 909):
            fake = FakeUpdater(fill=chip_image())
            fake.fail_after = cut
            with pytest.raises(p.GrainMediaError, match="cable"):
                await GrainMediaRecovery(fake).write([Segment("boot", 0, boot_image())], 8 * MIB)
            assert not p.has_system_header(bytes(fake.flash[:256])), cut

    async def test_nothing_is_written_when_reading_fails(self):
        fake = FakeUpdater(fill=chip_image())
        fake.can_read = False
        with pytest.raises(p.GrainMediaError, match="nothing has been written"):
            await GrainMediaRecovery(fake).write([Segment("boot", 0, boot_image())], 8 * MIB)
        assert fake.log == []

    async def test_a_write_protected_chip_is_noticed(self):
        fake = FakeUpdater(fill=chip_image())
        fake.locked = True
        with pytest.raises(p.GrainMediaError, match="flash differs"):
            await GrainMediaRecovery(fake).write([Segment("boot", 0, boot_image())], 8 * MIB)
        assert ("program", 0) not in fake.log

    async def test_a_chip_smaller_than_claimed_gets_no_header(self):
        fake = FakeUpdater(8 * MIB)
        with pytest.raises(p.GrainMediaError, match="flash differs"):
            await GrainMediaRecovery(fake).write(
                [Segment("flash", 0, chip_image(16 * MIB))], 16 * MIB
            )
        assert fake.log.count(("program", 0)) == 0

    def test_segments_are_checked(self):
        with pytest.raises(p.GrainMediaError, match="overlaps"):
            check_segments([Segment("a", 0, b"x" * 70000), Segment("b", 0x10000, b"x")], MIB)
        with pytest.raises(p.GrainMediaError, match="past"):
            check_segments([Segment("a", MIB, b"x")], MIB)
        with pytest.raises(ValueError):
            Segment("a", 0x100, b"x")


class TestProfile:
    def test_gm8126(self):
        profile = load_profile("gm8126")
        assert (profile.recovery, profile.usb_recovery_ids) == ("usb", [0x5677])
        assert usb_family("gm8126") == "grainmedia"
        assert usb_family("rv1106") == "rockusb"
        offsets = {n: e.lba * 512 for n, e in profile.partitions.items()}
        assert offsets == {"boot": 0, "env": 0x40000, "kernel": 0x50000, "rootfs": 0x250000}

    def test_grainmedia_needs_ids_and_partitions(self):
        base = {"name": "x", "RECOVERY": "usb", "USB_FAMILY": "grainmedia"}
        with pytest.raises(ValueError, match="USB_RECOVERY_IDS"):
            SoCProfile.model_validate(base)
        with pytest.raises(ValueError, match="partitions"):
            SoCProfile.model_validate({**base, "USB_RECOVERY_IDS": [1]})

    def test_layout_needs_room(self):
        with pytest.raises(p.GrainMediaError, match="more than"):
            install_segments(load_profile("gm8126"), {"env"}, 4 * MIB, bundle=None,
                             uboot=None, wipe_env=True, wipe_rootfs_data=False)

    def test_restore_is_whole_chip_only(self):
        with pytest.raises(p.GrainMediaError, match="whole-chip"):
            restore_segments(boot_image(), 8 * MIB)
        with pytest.raises(p.GrainMediaError, match="vendor's byte order"):
            restore_segments(p.swap_words(chip_image()), 8 * MIB)
        with pytest.raises(p.GrainMediaError, match="system header"):
            restore_segments(b"\xff" * 8 * MIB, 8 * MIB)


def only_backup(tmp_path: Path) -> bytes:
    (path,) = tmp_path.glob("gm8126-backup-*.bin")
    return path.read_bytes()


class TestCommands:
    def request(self, tmp_path: Path, **kw: object) -> InstallRequest:
        (tmp_path / "u-boot.bin").write_bytes(boot_image())
        fields = {
            "chip": "gm8126", "firmware_path": firmware(tmp_path),
            "uboot_path": str(tmp_path / "u-boot.bin"), "output": "json", **kw,
        }
        return InstallRequest(**fields)  # type: ignore[arg-type]

    async def test_install(self, board, tmp_path, capsys):
        before = bytes(board.flash)
        await cli.install(self.request(tmp_path))
        assert only_backup(tmp_path) == before
        assert board.flash[:0x38930] == boot_image()
        assert board.flash[0x40000:0x50000] == before[0x40000:0x50000], "env kept"
        assert board.flash[0x50000:0x50004] == b"\x27\x05\x19\x56"
        assert board.flash[0x250000:0x250004] == b"hsqs"
        assert board.flash[0x750000:] == before[0x750000:], "rootfs_data kept"
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert events[-1]["success"] is True

    async def test_install_wipes_on_request(self, board, tmp_path):
        await cli.install(self.request(tmp_path, wipe_env=True, wipe_rootfs_data=True))
        assert set(board.flash[0x40000:0x50000]) == {0xFF}
        assert set(board.flash[0x750000:]) == {0xFF}

    async def test_one_stage(self, board, tmp_path):
        before = bytes(board.flash)
        await cli.install(self.request(tmp_path, stages=("kernel",)))
        assert board.flash[:0x50000] == before[:0x50000]
        assert board.flash[0x250000:] == before[0x250000:]

    @pytest.mark.parametrize("kw,match", [
        ({"nand": True}, "NOR only"),
        ({"stages": ("reset",)}, "nothing to write"),
        ({"stages": ("env",)}, "nothing to write"),
        ({"stages": ("kernel",), "wipe_env": True}, "needs the env stage"),
        ({"skip_stages": ("rootfs-data",), "wipe_rootfs_data": True}, "rootfs-data stage"),
        ({"nor_size": 4}, "more than"),
        ({"nor_size": 16}, "repeats"),
        ({"nor_size": 32}, "16 MiB"),
    ])
    async def test_install_refusals_write_nothing(self, board, tmp_path, kw, match):
        with pytest.raises(p.GrainMediaError, match=match):
            await cli.install(self.request(tmp_path, **kw))
        assert not [e for e in board.log if e[0] != "geometry"]

    async def test_another_boards_firmware(self, board, tmp_path):
        request = self.request(tmp_path)
        firmware(tmp_path, "gm8136")
        with pytest.raises(p.GrainMediaError, match="not built for gm8126"):
            await cli.install(request)
        assert board.log == []

    async def test_erased_chip_needs_a_size(self, board, tmp_path):
        board.flash[:] = b"\xff" * board.size
        with pytest.raises(p.GrainMediaError, match="--nor-size"):
            await cli.install(self.request(tmp_path))
        await cli.install(self.request(tmp_path, nor_size=8))
        assert p.has_system_header(bytes(board.flash[:256]))

    async def test_failed_write_names_the_backup(self, board, tmp_path):
        board.fail_after = 10
        with pytest.raises(p.GrainMediaError, match="gm8126-backup-"):
            await cli.install(self.request(tmp_path))

    async def test_dump(self, board, tmp_path):
        async def dump(**kw: object) -> bytes:
            args = {"size": "", "partition": "", "wait": 1.0, "usb_path": "",
                    "power_cycle": False, "output": "json", **kw}
            await cli.dump_flash("gm8126", "out.bin", **args)  # type: ignore[arg-type]
            return (tmp_path / "out.bin").read_bytes()

        assert await dump() == board.flash
        assert await dump(partition="kernel") == board.flash[0x50000:0x250000]
        with pytest.raises(p.GrainMediaError, match="no partition"):
            await dump(partition="nope")
        assert not list(tmp_path.glob("*.partial"))

    async def restore(self, path: Path) -> None:
        await cli.restore("gm8126", str(path), wait=1.0, usb_path="",
                          power_cycle=False, poe_port="", output="json")

    async def test_restore(self, board, tmp_path):
        before = bytes(board.flash)
        board.flash[0x100000:0x200000] = b"\0" * MIB
        (tmp_path / "in.bin").write_bytes(before)
        await self.restore(tmp_path / "in.bin")
        assert board.flash == before

    async def test_restore_wrong_size(self, board, tmp_path):
        (tmp_path / "in.bin").write_bytes(chip_image(4 * MIB))
        with pytest.raises(p.GrainMediaError, match="and the flash"):
            await self.restore(tmp_path / "in.bin")
        assert not [e for e in board.log if e[0] != "geometry"]

    async def test_backups_never_replace_each_other(self, board, tmp_path):
        (tmp_path / "in.bin").write_bytes(bytes(board.flash))
        await self.restore(tmp_path / "in.bin")
        await self.restore(tmp_path / "in.bin")
        assert len(list(tmp_path.glob("gm8126-backup-*.bin"))) == 2

    def test_cli_dispatch(self, board, tmp_path):
        from typer.testing import CliRunner

        from defib.cli.app import app

        run = CliRunner().invoke
        assert run(app, ["dump-flash", "-c", "gm8126", "-o", "o.bin"]).exit_code == 0
        assert (tmp_path / "o.bin").read_bytes() == board.flash
        assert run(app, ["restore", "-c", "gm8126", "-i", "o.bin"]).exit_code == 0
        burn = run(app, ["burn", "-c", "gm8126", "--output", "json"])
        assert burn.exit_code == 1 and "no RAM boot" in burn.output
        self.request(tmp_path)
        result = run(app, ["install", "-c", "gm8126", "--firmware", "fw.tgz",
                           "--uboot", "u-boot.bin"])
        assert result.exit_code == 0, result.output
        bad = run(app, ["install", "-c", "gm8126", "--firmware", "absent.tgz",
                        "--uboot", "u-boot.bin", "--output", "json"])
        assert bad.exit_code == 1 and '"error"' in bad.output

    @pytest.mark.parametrize("size,want", [
        ("", 0), ("8MB", 8 * MIB), ("4mb", 4 * MIB), ("0x1000000", 16 * MIB),
    ])
    def test_sizes(self, size, want):
        assert cli.parse_size(size) == want

    @pytest.mark.parametrize("size", ["32MB", "3MB", "x", "-1", "1"])
    def test_bad_sizes(self, size):
        with pytest.raises(p.GrainMediaError):
            cli.parse_size(size)


class Pipe(Exception):
    errno = 32


class FakeEndpoint:
    wMaxPacketSize = 512

    def __init__(self, reads=(), writes=()):
        self.reads, self.writes = list(reads), list(writes)
        self.sent: list[bytes] = []
        self.cleared = 0

    def clear_halt(self):
        self.cleared += 1

    def write(self, data, _timeout):
        if self.writes:
            result = self.writes.pop(0)
            if isinstance(result, Exception):
                raise result
        self.sent.append(bytes(data))
        return len(data)

    def read(self, length, _timeout):
        assert length % self.wMaxPacketSize == 0
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestTransport:
    @pytest.fixture(autouse=True)
    def tag(self, monkeypatch):
        monkeypatch.setattr(device.secrets, "randbits", lambda _: 5)

    def make(self, reads, writes=()):
        dev = device.GrainMediaDevice(SimpleNamespace(handle=None))  # type: ignore[arg-type]
        dev._ep_in, dev._ep_out = FakeEndpoint(reads), FakeEndpoint(writes=writes)
        return dev

    @staticmethod
    def csw(status=0, residue=0, tag=5):
        return struct.pack("<4sIIB", b"USBS", tag, residue, status)

    def test_read(self):
        dev = self.make([b"\xff\0\xff\0\xff", self.csw()])
        assert dev.command(p.read_flash_id()) == p.FLASH_ID_SPI

    def test_write(self):
        dev = self.make([self.csw()])
        dev.command(p.program_page(1), b"x" * 256)
        assert [len(s) for s in dev._ep_out.sent] == [31, 256]

    def test_payload_length_is_fixed(self):
        with pytest.raises(ValueError):
            self.make([]).command(p.program_page(1), b"x")

    @pytest.mark.parametrize("reads", [
        [b"\0" * 5, struct.pack("<4sIIB", b"USBS", 5, 0, 1)],
        [b"\0" * 5, struct.pack("<4sIIB", b"USBS", 5, 2, 0)],
        [b"\0" * 3, struct.pack("<4sIIB", b"USBS", 5, 0, 0)],
    ])
    def test_failures_keep_the_session(self, reads):
        dev = self.make([*reads, b"\1" * 5, self.csw()])
        with pytest.raises(device.GrainMediaUsbError):
            dev.command(p.read_flash_id())
        assert dev.command(p.read_flash_id()) == b"\1" * 5

    def test_a_stall_is_cleared_and_reported(self):
        dev = self.make([Pipe(), self.csw(1), b"\1" * 5, self.csw()])
        with pytest.raises(device.GrainMediaUsbError, match="refused"):
            dev.command(p.read_flash_id())
        assert dev._ep_in.cleared == 1
        assert dev.command(p.read_flash_id()) == b"\1" * 5

    def test_a_stall_is_never_success(self):
        dev = self.make([self.csw(0)], writes=[31, Pipe()])
        with pytest.raises(device.GrainMediaUsbError, match="refused"):
            dev.command(p.program_page(1), b"x" * 256)
        assert dev._ep_out.cleared == 1

    def test_a_lost_status_ends_the_session(self):
        dev = self.make([TimeoutError("timeout")])
        with pytest.raises(device.GrainMediaUsbError):
            dev.command(p.erase_block(0))
        with pytest.raises(device.GrainMediaUsbError, match="power-cycle"):
            dev.command(p.erase_block(0))
        assert len(dev._ep_out.sent) == 1


class TestEnumeration:
    @pytest.fixture
    def bus(self, monkeypatch):
        usb = pytest.importorskip("usb.core")
        devices = []
        seen = {}

        def find(**kw):
            seen.update(kw)
            return iter(devices)

        monkeypatch.setattr(usb, "find", find)

        def add(pid, port):
            devices.append(SimpleNamespace(idProduct=pid, bus=1, address=port,
                                           port_numbers=(port,)))
        add.seen = seen
        return add

    def test_only_the_updater_id(self, bus):
        bus(0x1111, 1)
        assert device.find_device([0x5677]) is None
        bus(0x5677, 2)
        found = device.find_device([0x5677])
        assert found is not None and found.usb_path == "1-2"
        assert bus.seen["idVendor"] == 0x1233

    def test_two_need_a_path(self, bus):
        bus(0x5677, 1)
        bus(0x5677, 2)
        with pytest.raises(device.GrainMediaUsbError, match="--usb-path"):
            device.find_device([0x5677])
        assert device.find_device([0x5677], "1-2").address == 2

    def test_timeout_says_what_to_do(self, bus):
        with pytest.raises(device.GrainMediaUsbError, match="boot straps"):
            asyncio.run(device.wait_for_device([0x5677], timeout=0))
