"""Split NOR install for the gk7205v200 family (OpenIPC/u-boot openipc-hisilicon).

openipc.<board>-nor-<variant>-split.tgz carries one firmware.bin: a hashed FIT,
then the squashfs at the next 64 KiB block. It is written at 0x50000 and the
rest of the chip is erased; the kernel divides it into kernel, rootfs and
rootfs_data at boot. That U-Boot works out the NOR layout itself, so the env
stage loads its defaults rather than writing fixed-slot mtdparts.
"""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

import pytest
import typer

from defib.install import InstallRequest
from defib.install.firmware import load_firmware_bundle, split_image_end
from tests.test_nand_ubi_install import FACTORY_MAC, _patch_install, _write_tar

BLOCK = 0x10000
CHIP_8M = 8 * 1024 * 1024


def _split_image(fit_size: int = 0x18000, squashfs_used: int = 0x2F000) -> bytes:
    """A FIT whose totalsize rounds up to 0x20000, then a squashfs there."""
    fit = b"\xd0\x0d\xfe\xed" + fit_size.to_bytes(4, "big")
    fit = fit.ljust((fit_size + BLOCK - 1) // BLOCK * BLOCK, b"\xab")
    squashfs = b"hsqs" + b"\x00" * 36 + squashfs_used.to_bytes(8, "little")
    squashfs = squashfs.ljust((squashfs_used + BLOCK - 1) // BLOCK * BLOCK, b"\xcd")
    return fit + squashfs


def _split_package(path: Path, board: str = "gk7205v200", image: bytes | None = None,
                   md5: str | None = None) -> Path:
    image = _split_image() if image is None else image
    digest = md5 or hashlib.md5(image).hexdigest()
    _write_tar(path, {
        f"firmware.bin.{board}.md5sum": f"{digest}  firmware.bin.{board}\n".encode(),
        f"firmware.bin.{board}": image,
    })
    return path


class FakeNorUBoot:
    """An OpenIPC/u-boot NOR console over a byte-accurate 8 MiB chip."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.tftp_files: dict[str, bytes] = {}
        self.ram = b""
        self.flash = bytearray(b"\x5a" * CHIP_8M)  # an old layout's leftovers
        self.env: dict[str, str] = {"ethaddr": FACTORY_MAC, "mtdparts": "saved"}

    def reply(self, command: str) -> str:
        return "".join(self._one(part.strip()) for part in command.split(";")) or "\nOpenIPC # "

    def _one(self, command: str) -> str:
        self.commands.append(command)
        prompt = "\nOpenIPC # "
        words = command.split()
        if not words:
            return ""
        if command == "sf probe 0":
            return 'SF: Detected XM25QH64C with page size 256 Bytes, Block:64KB Chip:8MB' + prompt
        if words[0] in ("tftpboot", "tftp"):
            self.ram = self.tftp_files[words[-1]]
            return f"Bytes transferred = {len(self.ram)} (hex)" + prompt
        if words[0] == "crc32":
            size = int(words[2], 16)
            return f"crc32 for {words[1]} ... ==> {zlib.crc32(self.ram[:size]):08x}" + prompt
        if words[:2] == ["sf", "erase"]:
            off, size = int(words[2], 16), int(words[3], 16)
            assert off + size <= CHIP_8M
            self.flash[off:off + size] = b"\xff" * size
            return f"SF: {size} bytes @ {off:#x} Erased: OK" + prompt
        if words[:2] == ["sf", "write"]:
            off, size = int(words[3], 16), int(words[4], 16)
            self.flash[off:off + size] = self.ram[:size]
            return f"SF: {size} bytes @ {off:#x} Written: OK" + prompt
        if words[:2] == ["sf", "read"]:
            off, size = int(words[3], 16), int(words[4], 16)
            self.ram = bytes(self.flash[off:off + size])
            return f"SF: {size} bytes @ {off:#x} Read: OK" + prompt
        if words[0] == "printenv":
            key = words[1]
            if key in self.env:
                return f"{key}={self.env[key]}" + prompt
            return f'## Error: "{key}" not defined' + prompt
        if words[0] == "setenv":
            self.env[words[1]] = " ".join(words[2:])
            return prompt
        if command == "env default -a":
            self.env = {"bootcmd": "run bootflash"}
            return "## Resetting to default environment" + prompt
        if command == "saveenv":
            return "Saving Environment to SPI Flash... OK" + prompt
        if command == "reset":
            return "resetting ..."
        return prompt


def _seed_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("sys.platform", "linux")
    from defib.firmware import get_cache_dir

    cache = get_cache_dir()
    for soc in ("gk7205v200", "hi3516ev300"):
        (cache / f"u-boot-{soc}-nor.bin").write_bytes(b"N" * 0x36000)
        (cache / f"u-boot-{soc}-universal.bin").write_bytes(b"X" * 0x30000)


async def _install(monkeypatch, tmp_path, firmware: Path, *, chip: str = "gk7205v200",
                   **kwargs) -> tuple[FakeNorUBoot, dict[str, object]]:
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNorUBoot()
    seen = _patch_install(monkeypatch, device)
    await run_install(InstallRequest(
        chip=chip, firmware_path=str(firmware), port="COM15", nic="eth0",
        output="json", **kwargs,
    ))
    return device, seen


# --- the package --------------------------------------------------------------

def test_split_package_is_recognised(tmp_path):
    bundle = load_firmware_bundle(_split_package(tmp_path / "fw.tgz"), boards={"gk7205v200"})
    assert bundle.split
    assert bundle.rootfs_name == "firmware.bin.gk7205v200"
    assert bundle.rootfs == _split_image()
    assert bundle.kernel == b""
    assert split_image_end(bundle.rootfs) == 0x50000


def test_split_package_md5_mismatch(tmp_path):
    with pytest.raises(ValueError, match="MD5 mismatch"):
        load_firmware_bundle(_split_package(tmp_path / "fw.tgz", md5="0" * 32))


def test_split_package_for_another_board(tmp_path):
    with pytest.raises(ValueError, match="built for gk7205v300"):
        load_firmware_bundle(
            _split_package(tmp_path / "fw.tgz", board="gk7205v300"), boards={"gk7205v200"}
        )


@pytest.mark.parametrize("image, reason", [
    (b"\x27\x05\x19\x56" + b"\x00" * 0x1FFFC, "does not start with a FIT"),
    (_split_image()[:0x20000] + b"\x00" * 0x30000, "no squashfs"),
    (_split_image() + b"\x00" * BLOCK, "runs past the end of its squashfs"),
])
def test_split_package_shape_is_checked(tmp_path, image, reason):
    with pytest.raises(ValueError, match=reason):
        load_firmware_bundle(_split_package(tmp_path / "fw.tgz", image=image))


# --- the install --------------------------------------------------------------

@pytest.mark.asyncio
async def test_split_install_writes_firmware_bin_over_the_whole_chip(monkeypatch, tmp_path):
    firmware = _split_package(tmp_path / "openipc.gk7205v200-nor-lite-split.tgz")
    device, seen = await _install(monkeypatch, tmp_path, firmware)

    assert Path(str(seen["burned"])).name == "u-boot-gk7205v200-nor.bin"
    assert sorted(device.tftp_files) == ["r", "u"]
    assert seen["done_count"] == 2
    image = _split_image()
    assert bytes(device.flash[0x50000:0x50000 + len(image)]) == image
    # Nothing of the old layout is left for U-Boot's scan or the overlay.
    assert bytes(device.flash[0x50000 + len(image):]) == b"\xff" * (CHIP_8M - 0x50000 - len(image))
    assert "sf erase 0x50000 0x7b0000" in device.commands
    # U-Boot's own defaults, the MAC carried across; no fixed-slot env.
    assert "env default -a" in device.commands
    assert device.env.get("ethaddr") == FACTORY_MAC
    assert not any(c.startswith("setenv mtdparts") for c in device.commands)
    assert not any("bootcmdnor" in c for c in device.commands)


@pytest.mark.asyncio
async def test_fixed_slot_install_on_this_uboot_keeps_its_bootcmd(monkeypatch, tmp_path):
    """The openipc-hisilicon environment has no bootcmdnor to point bootcmd at."""
    firmware = tmp_path / "openipc.gk7205v200-nor-lite.tgz"
    _write_tar(firmware, {"uImage.gk7205v200": b"K" * 1024,
                          "rootfs.squashfs.gk7205v200": b"R" * 2048})
    device, _ = await _install(monkeypatch, tmp_path, firmware)

    assert bytes(device.flash[0x50000:0x50400]) == b"K" * 1024
    assert bytes(device.flash[0x250000:0x250800]) == b"R" * 2048
    assert "env default -a" in device.commands
    assert device.env.get("bootcmd") == "run bootflash"
    assert not any("bootcmdnor" in c for c in device.commands)


@pytest.mark.asyncio
async def test_split_kernel_and_rootfs_data_stages_write_nothing(monkeypatch, tmp_path):
    firmware = _split_package(tmp_path / "fw.tgz")
    device, seen = await _install(monkeypatch, tmp_path, firmware,
                                  stages=("kernel", "rootfs-data"))
    assert not any(c.startswith(("sf erase", "sf write")) for c in device.commands)
    assert "done_count" not in seen


@pytest.mark.asyncio
async def test_split_image_too_big_for_the_chip(monkeypatch, tmp_path):
    image = _split_image(fit_size=0x18000, squashfs_used=0x7B0000 - 0x20000)  # ends at the chip end
    firmware = _split_package(tmp_path / "fw.tgz", image=image)
    with pytest.raises(typer.Exit):
        await _install(monkeypatch, tmp_path, firmware)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, code", [
    ({"chip": "hi3516ev300"}, 2),
    # NAND here is the UBI layout, whose package loader names the mistake.
    ({"nand": True}, 1),
    ({"wipe_rootfs_data": True, "stages": ("rootfs-data",)}, 2),
])
async def test_split_install_refusals(monkeypatch, tmp_path, capsys, kwargs, code):
    board = "hi3516ev300" if kwargs.get("chip") == "hi3516ev300" else "gk7205v200"
    firmware = _split_package(tmp_path / "fw.tgz", board=board)
    with pytest.raises(typer.Exit) as exc:
        await _install(monkeypatch, tmp_path, firmware, **kwargs)
    assert exc.value.exit_code == code
    if kwargs.get("nand"):
        assert "cannot be installed on NAND" in capsys.readouterr().out
