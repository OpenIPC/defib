"""Tests for NAND flash install support and protocol robustness fixes."""

from __future__ import annotations

import io
import tarfile
from contextlib import asynccontextmanager

import pytest

from defib.cli.app import (
    _NAND_LAYOUT,
    _NOR8M_LAYOUT,
    _NOR16M_LAYOUT,
    _nand_bootargs,
)


class TestNandLayout:
    """Verify NAND partition layout constants."""

    def test_nand_layout_partitions_exist(self):
        for key in ("boot", "env", "kernel", "rootfs"):
            assert key in _NAND_LAYOUT

    def test_nand_layout_offsets_contiguous(self):
        """Partitions must not overlap and boot+env+kernel must be contiguous."""
        b_off, b_sz = _NAND_LAYOUT["boot"]
        e_off, e_sz = _NAND_LAYOUT["env"]
        k_off, k_sz = _NAND_LAYOUT["kernel"]
        r_off, _r_sz = _NAND_LAYOUT["rootfs"]

        assert b_off == 0
        assert e_off == b_off + b_sz
        assert k_off == e_off + e_sz
        assert r_off == k_off + k_sz

    def test_nand_boot_env_sizes(self):
        """Boot and env are 1MB each (NAND erase-block aligned)."""
        assert _NAND_LAYOUT["boot"] == (0x000000, 0x100000)
        assert _NAND_LAYOUT["env"] == (0x100000, 0x100000)

    def test_nand_kernel_8mb(self):
        assert _NAND_LAYOUT["kernel"] == (0x200000, 0x800000)

    def test_nand_rootfs_starts_at_10mb(self):
        r_off, r_sz = _NAND_LAYOUT["rootfs"]
        assert r_off == 0xA00000  # 10MB
        assert r_sz > 0

    def test_nand_layout_larger_than_nor(self):
        """NAND partitions must be larger than NOR equivalents."""
        for key in ("boot", "env", "kernel", "rootfs"):
            _, nand_sz = _NAND_LAYOUT[key]
            _, nor_sz = _NOR8M_LAYOUT[key]
            assert nand_sz >= nor_sz, f"NAND {key} smaller than NOR 8M"

    def test_nor_layouts_unchanged(self):
        """Regression: NOR layouts must not be modified."""
        assert _NOR8M_LAYOUT["boot"] == (0x000000, 0x40000)
        assert _NOR8M_LAYOUT["kernel"] == (0x050000, 0x200000)
        assert _NOR16M_LAYOUT["boot"] == (0x000000, 0x40000)
        assert _NOR16M_LAYOUT["kernel"] == (0x050000, 0x300000)


class TestNandBootargs:
    """Verify defib sets bootargs that match the rootfs format we flash.

    Regression: U-Boot's compiled-in default bootargs is unreliable.  Recent
    OpenIPC builds default to ``rootfstype=squashfs`` even when the actual
    rootfs.ubi contains UBIFS, causing kernel panic ("Unable to mount root
    fs").  defib must set bootargs explicitly to match what it wrote.
    """

    def test_ubifs_bootargs_uses_ubi_root(self):
        """UBIFS rootfs: use ubi0:rootfs (kernel attaches the UBI volume)."""
        args = _nand_bootargs(rootfs_is_ubi=True)
        assert "root=ubi0:rootfs" in args
        assert "rootfstype=ubifs" in args
        # Must not contain squashfs-specific bits
        assert "squashfs" not in args
        assert "ubiblock" not in args
        assert "ubi.block" not in args

    def test_squashfs_bootargs_uses_ubiblock(self):
        """Non-UBI rootfs: use ubiblock0_0 with squashfs filesystem type."""
        args = _nand_bootargs(rootfs_is_ubi=False)
        assert "root=/dev/ubiblock0_0" in args
        assert "rootfstype=squashfs" in args
        assert "ubi.block=0,0" in args
        # Must not contain UBIFS-specific root
        assert "ubi0:rootfs" not in args

    def test_bootargs_matches_mtdparts_layout(self):
        """ubi.mtd index must match the partition layout (boot,env,kernel,ubi
        → mtd3 = ubi).  Otherwise UBI attaches the wrong partition."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "ubi.mtd=3,2048" in args, args
            assert (
                "mtdparts=hinand:1024k(boot),1024k(env),"
                "8192k(kernel),-(ubi)"
            ) in args, args

    def test_bootargs_includes_console_and_panic(self):
        """Standard fields needed for a usable rescue boot — serial console
        for debugging and panic-reboot so a broken boot doesn't hang."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "console=ttyAMA0,115200" in args
            assert "panic=20" in args

    def test_bootargs_is_single_line(self):
        """No newlines or null bytes — saveenv must store it as a single
        bootargs= line."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "\n" not in args
            assert "\x00" not in args
            assert "\r" not in args


@pytest.mark.asyncio
async def test_nand_install_without_crc32_keeps_legacy_compatibility(
    monkeypatch, tmp_path, capsys
):
    import defib.flashdump
    import defib.network.ip_manager
    import defib.network.tftp_server
    import defib.recovery.session
    import defib.transport.serial_platform
    from defib.install import InstallRequest
    from defib.install.orchestrator import run_install
    from defib.recovery.events import RecoveryResult
    from defib.transport.base import Transport, TransportTimeout

    firmware_tar = tmp_path / "firmware.tgz"
    kernel = b"K" * 1024
    rootfs = b"R" * 2048
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv300", kernel),
            ("rootfs.squashfs.hi3516cv300", rootfs),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    uboot = tmp_path / "u-boot.bin"
    uboot.write_bytes(b"U" * 1024)

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

    transport = ShellTransport()
    commands: list[str] = []
    tftp_files: dict[str, bytes] = {}

    class FakeRecoverySession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, transport_obj, **kwargs):
            assert transport_obj is transport
            return RecoveryResult(success=True)

    async def fake_create_transport(port: str):
        assert port == "COM15"
        return transport

    @asynccontextmanager
    async def fake_temporary_ip(interface: str, ip: str, netmask: str):
        assert interface == "Ethernet"
        yield

    class FakeTFTPTransport:
        def close(self) -> None:
            pass

    class FakeTFTPProtocol:
        def __init__(self, files):
            self._files = dict(files)

        def set_max_blocksize(self, blocksize: int) -> None:
            raise AssertionError("no retry block-size change expected")

    async def fake_start_tftp_server(*, files, bind_addr, port, done_count):
        assert done_count == 2
        tftp_files.clear()
        tftp_files.update(files)
        return FakeTFTPTransport(), FakeTFTPProtocol(files)

    async def fake_send_command(
        transport_obj,
        command: str,
        timeout: float = 0.0,
        **kwargs,
    ) -> str:
        assert transport_obj is transport
        commands.append(command)

        if command == "nand info":
            return "Device 0: nand0, sector size 128 KiB\nOpenIPC # "
        if command.startswith(("tftpboot ", "tftp ")):
            name = command.split()[-1]
            return f"Bytes transferred = {len(tftp_files[name])}\nOpenIPC # "
        if command.startswith("crc32 "):
            return "Unknown command 'crc32'\nOpenIPC # "
        if command.startswith(("nand erase ", "nand write ")):
            return "OK\nOpenIPC # "
        return "OpenIPC # "

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)
    monkeypatch.setattr(defib.recovery.session, "RecoverySession", FakeRecoverySession)
    monkeypatch.setattr(
        defib.transport.serial_platform,
        "create_transport",
        fake_create_transport,
    )
    monkeypatch.setattr(
        defib.transport.serial_platform,
        "normalize_port_name",
        lambda port: port,
    )
    monkeypatch.setattr(
        defib.network.ip_manager,
        "temporary_ip",
        fake_temporary_ip,
    )
    monkeypatch.setattr(
        defib.network.tftp_server,
        "start_tftp_server",
        fake_start_tftp_server,
    )

    await run_install(
        InstallRequest(
            chip="hi3516cv300",
            firmware_path=str(firmware_tar),
            uboot_path=str(uboot),
            port="COM15",
            nic="Ethernet",
            nand=True,
            stages=("kernel", "rootfs"),
            output="json",
        )
    )

    assert commands.count("crc32 0x82000000 0x400") == 1
    assert "tftpboot 0x82000000 k" in commands
    assert "tftpboot 0x82000000 r" in commands
    assert any(command.startswith("nand erase ") for command in commands)
    assert any(command.startswith("nand write ") for command in commands)
    assert capsys.readouterr().out.count("crc32 is unavailable") == 1
    assert transport.closed is True
