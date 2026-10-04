"""UBI-only NAND install for the u-boot-xmedia SoCs (hi3516ev300 & co.).

Layout: 0x000000 boot 768K, 0x0C0000 env 256K, 0x100000 ubi to the chip end.
The NAND U-Boot's default environment owns mtdparts/bootcmd/bootargs, so the
installer must only write U-Boot and the UBI image, and carry the MAC across.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import typer

from defib.firmware import pad_to_size
from defib.install import InstallRequest
from defib.install.firmware import load_firmware_bundle
from defib.install.layout import (
    NAND_UBI_LAYOUT,
    NAND_UBI_OFFSET,
    mtdparts_partition_offset,
    parse_nand_erase_range,
    uboot_reports_ok,
)
from defib.recovery.events import RecoveryResult
from defib.transport.base import Transport, TransportTimeout

RAM = 0x42000000
UBI_IMAGE = b"UBI#" + b"\x01" * 0x1FFFC + b"\xff" * 0x20000  # two 128 KiB PEBs
FACTORY_MAC = "00:12:34:56:78:9a"
XMEDIA_MTDPARTS = "mtdparts=hinand:768k(boot),256k(env),-(ubi)"


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def _nand_package(path: Path, board: str = "hi3516ev300") -> Path:
    """Shape of openipc.<board>-nand-<variant>.tgz: no uImage, no squashfs."""
    fit = b"F" * 4096
    ubifs = b"\x31\x18\x10\x06" + b"\x00" * 4092
    members = {
        f"fitImage.{board}": fit,
        f"fitImage.{board}.md5sum": f"{hashlib.md5(fit).hexdigest()}  fitImage.{board}\n".encode(),
        f"rootfs.ubifs.{board}": ubifs,
        f"rootfs.ubifs.{board}.md5sum": (
            f"{hashlib.md5(ubifs).hexdigest()}  rootfs.ubifs.{board}\n".encode()
        ),
        f"rootfs.ubi.{board}": UBI_IMAGE,
        f"rootfs.ubi.{board}.md5sum": (
            f"{hashlib.md5(UBI_IMAGE).hexdigest()}  rootfs.ubi.{board}\n".encode()
        ),
    }
    _write_tar(path, members)
    return path


class ShellTransport(Transport):
    def __init__(self) -> None:
        self.rx = bytearray()
        self.closed = False

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if not self.rx:
            raise TransportTimeout("no data")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def write(self, data: bytes) -> None:
        if b"\x03" in data:
            self.rx.extend(b"OpenIPC # ")

    async def flush_input(self) -> None:
        self.rx.clear()

    async def flush_output(self) -> None:
        return None

    async def bytes_waiting(self) -> int:
        return len(self.rx)

    async def close(self) -> None:
        self.closed = True


class FakeNandUBoot:
    """Just enough of a u-boot-xmedia NAND console to drive run_install."""

    def __init__(
        self,
        *,
        mtdparts: str | None = XMEDIA_MTDPARTS,
        erase_part_supported: bool = True,
    ) -> None:
        self.commands: list[str] = []
        self.tftp_files: dict[str, bytes] = {}
        self.ram = b""
        self.flash: dict[int, bytes] = {}
        self.env: dict[str, str] = {"ethaddr": FACTORY_MAC}
        if mtdparts is not None:
            self.env["mtdparts"] = mtdparts
        self.erase_part_supported = erase_part_supported

    def reply(self, command: str) -> str:
        self.commands.append(command)
        prompt = "\nOpenIPC # "
        words = command.split()
        if command == "nand info":
            return "Device 0: nand0, sector size 128 KiB\n  Page size 2048 b" + prompt
        if words[0] in ("tftpboot", "tftp"):
            self.ram = self.tftp_files[words[-1]]
            return f"Bytes transferred = {len(self.ram)} (hex)" + prompt
        if words[0] == "crc32":
            size = int(words[2], 16)
            crc = zlib.crc32(self.ram[:size]) & 0xFFFFFFFF
            return f"crc32 for {words[1]} ... ==> {crc:08x}" + prompt
        if words[0] == "printenv":
            key = words[1]
            if key in self.env:
                return f"{key}={self.env[key]}" + prompt
            return f'## Error: "{key}" not defined' + prompt
        if words[0] == "setenv":
            self.env[words[1]] = " ".join(words[2:])
            return prompt
        if command == "env default -a":
            self.env = {"mtdparts": XMEDIA_MTDPARTS, "bootcmd": "default"}
            return "## Resetting to default environment" + prompt
        if command == "nand erase.part ubi":
            if not self.erase_part_supported:
                return "Usage:\nnand - NAND sub-system" + prompt
            return (
                "\nNAND erase.part: device 0 offset 0x100000, size 0x7f00000\n"
                "Erasing at 0x7fe0000 -- 100% complete.\nOK" + prompt
            )
        if command == "nand erase 0x100000":
            return (
                "\nNAND erase: device 0 offset 0x100000, size 0x7f00000\n"
                "Erasing at 0x7fe0000 -- 100% complete.\nOK" + prompt
            )
        if words[:2] == ["nand", "erase"]:
            return "Erasing at 0x0 -- 100% complete.\nOK" + prompt
        if words[0] == "nand" and words[1].startswith("write"):
            offset, size = int(words[3], 16), int(words[4], 16)
            self.flash[offset] = self.ram[:size]
            return f" {size} bytes written: OK" + prompt
        if words[:2] == ["nand", "read"]:
            offset, size = int(words[3], 16), int(words[4], 16)
            self.ram = self.flash[offset][:size].ljust(size, b"\xff")
            return f" {size} bytes read: OK" + prompt
        if command == "saveenv":
            return "Saving Environment to NAND...\nWriting to NAND... OK" + prompt
        if command == "reset":
            return "resetting ..."
        return prompt


def _patch_install(monkeypatch, device: FakeNandUBoot) -> dict[str, object]:
    import defib.flashdump
    import defib.network.ip_manager
    import defib.network.tftp_server
    import defib.recovery.session
    import defib.transport.serial_platform

    transport = ShellTransport()
    seen: dict[str, object] = {"transport": transport}

    class FakeRecoverySession:
        def __init__(self, *args, **kwargs) -> None:
            seen["burned"] = kwargs.get("firmware_path")

        async def run(self, transport_obj, **kwargs):
            assert transport_obj is transport
            return RecoveryResult(success=True)

    async def fake_create_transport(port: str):
        return transport

    @asynccontextmanager
    async def fake_temporary_ip(interface: str, ip: str, netmask: str):
        yield

    class FakeTFTPTransport:
        def close(self) -> None:
            pass

    class FakeTFTPProtocol:
        def __init__(self, files):
            self._files = files

        def set_max_blocksize(self, blocksize: int) -> None:
            raise AssertionError("no retry expected")

    async def fake_start_tftp_server(*, files, bind_addr, port, done_count):
        seen["done_count"] = done_count
        device.tftp_files = dict(files)
        return FakeTFTPTransport(), FakeTFTPProtocol(device.tftp_files)

    async def fake_send_command(transport_obj, command: str, timeout: float = 0.0, **kw):
        assert transport_obj is transport
        return device.reply(command)

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)
    monkeypatch.setattr(defib.recovery.session, "RecoverySession", FakeRecoverySession)
    monkeypatch.setattr(
        defib.transport.serial_platform, "create_transport", fake_create_transport
    )
    monkeypatch.setattr(
        defib.transport.serial_platform, "normalize_port_name", lambda port: port
    )
    monkeypatch.setattr(defib.network.ip_manager, "temporary_ip", fake_temporary_ip)
    monkeypatch.setattr(
        defib.network.tftp_server, "start_tftp_server", fake_start_tftp_server
    )
    return seen


def _seed_cache(monkeypatch, tmp_path: Path) -> Path:
    """Cache holding both builds plus a stale universal image."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("sys.platform", "linux")
    from defib.firmware import get_cache_dir

    cache = get_cache_dir()
    (cache / "u-boot-hi3516ev300-nor.bin").write_bytes(b"R" * 0x30000)
    (cache / "u-boot-hi3516ev300-nand.bin").write_bytes(b"N" * 0x40000)
    (cache / "u-boot-hi3516ev300-universal.bin").write_bytes(b"X" * 0x30000)
    return cache


# The exact console sequence a full hi3516ev300 NAND install sends (default
# stage plan: uboot, kernel, rootfs, rootfs-data, env, reset).
EXPECTED_HI3516EV300_NAND_SEQUENCE = [
    "nand info",
    "setenv ipaddr 192.168.1.20",
    "setenv serverip 192.168.1.10",
    # uboot: u-boot-hi3516ev300-nand.bin, padded host-side to 0xC0000
    f"tftpboot 0x{RAM:x} u",
    f"crc32 0x{RAM:x} 0xc0000",
    "nand erase 0x0 0xc0000",
    f"nand write 0x{RAM:x} 0x0 0xc0000",
    # kernel and rootfs-data: no-ops, the UBI image carries both
    # rootfs: the whole rootfs.ubi.<board>
    f"tftpboot 0x{RAM:x} r",
    f"crc32 0x{RAM:x} 0x40000",
    "printenv mtdparts",
    "nand erase.part ubi",
    f"nand write.trimffs 0x{RAM:x} 0x100000 0x40000",
    f"nand read 0x{RAM:x} 0x100000 0x40000",
    f"crc32 0x{RAM:x} 0x40000",
    # env: start from the new U-Boot's defaults, keep the camera's MAC
    "printenv ethaddr",
    "env default -a",
    "printenv ethaddr",
    f"setenv ethaddr {FACTORY_MAC}",
    "saveenv",
    "reset",
]


@pytest.mark.asyncio
async def test_hi3516ev300_nand_install_command_sequence(monkeypatch, tmp_path):
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot()
    seen = _patch_install(monkeypatch, device)

    await run_install(
        InstallRequest(
            chip="hi3516ev300",
            firmware_path=str(_nand_package(tmp_path / "openipc.hi3516ev300-nand-lite.tgz")),
            port="COM15",
            nic="eth0",
            nand=True,
            output="json",
        )
    )

    assert device.commands == EXPECTED_HI3516EV300_NAND_SEQUENCE
    joined = "\n".join(device.commands)
    for forbidden in (
        "setenv mtdparts", "setenv mtdids", "setenv bootcmd", "setenv bootargs",
        "ubi create", "ubi write", "ubi part",
    ):
        assert forbidden not in joined

    # Burned and installed the NAND build, never the stale universal image.
    assert Path(str(seen["burned"])).name == "u-boot-hi3516ev300-nand.bin"
    assert seen["done_count"] == 2
    assert set(device.tftp_files) == {"u", "r"}
    assert device.tftp_files["u"] == pad_to_size(b"N" * 0x40000, 0xC0000)
    assert device.tftp_files["r"] == UBI_IMAGE
    assert device.flash[0x100000] == UBI_IMAGE
    assert device.env["ethaddr"] == FACTORY_MAC
    assert seen["transport"].closed is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_nand_install_falls_back_to_erase_to_chip_end(monkeypatch, tmp_path):
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot(erase_part_supported=False)
    _patch_install(monkeypatch, device)

    await run_install(
        InstallRequest(
            chip="hi3516ev300",
            firmware_path=str(_nand_package(tmp_path / "fw.tgz")),
            port="COM15",
            nic="eth0",
            nand=True,
            stages=("rootfs",),
            output="json",
        )
    )

    erase_part = device.commands.index("nand erase.part ubi")
    fallback = device.commands.index("nand erase 0x100000")
    write = device.commands.index(f"nand write.trimffs 0x{RAM:x} 0x100000 0x40000")
    assert erase_part < fallback < write


@pytest.mark.asyncio
async def test_nand_install_without_mtdparts_erases_by_offset(monkeypatch, tmp_path):
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot(mtdparts=None)
    _patch_install(monkeypatch, device)

    await run_install(
        InstallRequest(
            chip="hi3516ev300",
            firmware_path=str(_nand_package(tmp_path / "fw.tgz")),
            port="COM15",
            nic="eth0",
            nand=True,
            stages=("rootfs",),
            output="json",
        )
    )

    assert "nand erase.part ubi" not in device.commands
    assert "nand erase 0x100000" in device.commands


@pytest.mark.asyncio
async def test_nand_install_refuses_a_foreign_ubi_offset(monkeypatch, tmp_path):
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot(
        mtdparts="mtdparts=hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)"
    )
    _patch_install(monkeypatch, device)

    with pytest.raises(typer.Exit):
        await run_install(
            InstallRequest(
                chip="hi3516ev300",
                firmware_path=str(_nand_package(tmp_path / "fw.tgz")),
                port="COM15",
                nic="eth0",
                nand=True,
                stages=("rootfs",),
                output="json",
            )
        )

    assert not any(c.startswith("nand erase") for c in device.commands)
    assert not any(c.startswith("nand write") for c in device.commands)


@pytest.mark.asyncio
async def test_env_only_stage_keeps_the_existing_environment(monkeypatch, tmp_path):
    """Without a new U-Boot there is no `env default -a`; the MAC stays."""
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot()
    _patch_install(monkeypatch, device)

    await run_install(
        InstallRequest(
            chip="hi3516ev300",
            firmware_path=str(_nand_package(tmp_path / "fw.tgz")),
            port="COM15",
            nand=True,
            stages=("env",),
            output="json",
        )
    )

    assert device.commands == ["nand info", "printenv ethaddr", "saveenv"]


@pytest.mark.asyncio
async def test_hi3516ev300_nor_install_uses_the_nor_build(monkeypatch, tmp_path):
    import defib.flashdump
    from defib.install.orchestrator import run_install

    _seed_cache(monkeypatch, tmp_path)
    device = FakeNandUBoot()
    seen = _patch_install(monkeypatch, device)
    nor_firmware = tmp_path / "openipc.hi3516ev300-nor-lite.tgz"
    _write_tar(
        nor_firmware,
        {"uImage.hi3516ev300": b"K" * 1024, "rootfs.squashfs.hi3516ev300": b"R" * 2048},
    )

    async def nor_send_command(transport_obj, command: str, timeout: float = 0.0, **kw):
        if command == "sf probe 0":
            device.commands.append(command)
            return 'Spi(cs1): Block:64KB Chip:8MB Name:"XT25F64B"\nOpenIPC # '
        if command.startswith("sf read"):
            device.commands.append(command)
            device.ram = device.tftp_files["u"]
            return "SF: done\nOpenIPC # "
        if command.startswith("sf "):
            device.commands.append(command)
            return "SF: done\nOpenIPC # "
        return device.reply(command)

    monkeypatch.setattr(defib.flashdump, "send_command", nor_send_command)

    await run_install(
        InstallRequest(
            chip="hi3516ev300",
            firmware_path=str(nor_firmware),
            port="COM15",
            nic="eth0",
            stages=("uboot",),
            output="json",
        )
    )

    assert Path(str(seen["burned"])).name == "u-boot-hi3516ev300-nor.bin"
    assert device.tftp_files["u"] == pad_to_size(b"R" * 0x30000, 0x40000)
    assert "sf erase 0x0 0x40000" in device.commands


def test_nand_package_selects_rootfs_ubi_not_ubifs(tmp_path):
    bundle = load_firmware_bundle(_nand_package(tmp_path / "fw.tgz"), ubi_only=True)
    assert bundle.rootfs_name == "rootfs.ubi.hi3516ev300"
    assert bundle.rootfs == UBI_IMAGE
    assert bundle.kernel == b""


def test_nand_package_for_gk7205v510_uses_gk7205v500_board(tmp_path):
    bundle = load_firmware_bundle(
        _nand_package(tmp_path / "fw.tgz", board="gk7205v500"), ubi_only=True
    )
    assert bundle.rootfs_name == "rootfs.ubi.gk7205v500"


def test_split_layout_package_is_rejected_for_ubi_layout(tmp_path):
    old = tmp_path / "old.tgz"
    _write_tar(old, {"uImage.hi3516ev300": b"K" * 64, "rootfs.squashfs.hi3516ev300": b"R" * 64})
    with pytest.raises(ValueError, match="rootfs.ubi"):
        load_firmware_bundle(old, ubi_only=True)


def test_ubi_md5_mismatch_is_rejected(tmp_path):
    bad = tmp_path / "bad.tgz"
    _write_tar(
        bad,
        {
            "rootfs.ubi.hi3516ev300": UBI_IMAGE,
            "rootfs.ubi.hi3516ev300.md5sum": b"0" * 32 + b"  rootfs.ubi.hi3516ev300\n",
        },
    )
    with pytest.raises(ValueError, match="MD5 mismatch"):
        load_firmware_bundle(bad, ubi_only=True)


class TestLayoutHelpers:
    def test_ubi_layout_constants(self):
        assert NAND_UBI_LAYOUT["boot"] == (0x0, 0xC0000)
        assert NAND_UBI_LAYOUT["env"] == (0xC0000, 0x40000)
        assert NAND_UBI_OFFSET == 0x100000

    @pytest.mark.parametrize(
        "value",
        [
            "mtdparts=hinand:768k(boot),256k(env),-(ubi)",
            "hinand:768k(boot),256k(env),-(ubi)",
            "nand:768k(boot),256k(env),-(ubi)",
            "spi:1m(x);nand:0xc0000(boot),0x40000(env),-(ubi)",
        ],
    )
    def test_ubi_offset_from_mtdparts(self, value):
        assert mtdparts_partition_offset(value, "ubi") == 0x100000

    def test_split_layout_ubi_offset(self):
        value = "hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)"
        assert mtdparts_partition_offset(value, "ubi") == 0xA00000

    def test_missing_partition(self):
        assert mtdparts_partition_offset("hinand:768k(boot),-(rest)", "ubi") is None

    def test_erase_range_and_ok(self):
        resp = (
            "NAND erase.part: device 0 offset 0x100000, size 0x7f00000\n"
            "Erasing at 0x7fe0000 -- 100% complete.\nOK\nOpenIPC # "
        )
        assert parse_nand_erase_range(resp) == (0x100000, 0x7F00000)
        assert uboot_reports_ok(resp)
        assert uboot_reports_ok(" 262144 bytes written: OK\nOpenIPC # ")
        assert not uboot_reports_ok("Usage:\nnand - NAND sub-system\nOpenIPC # ")
