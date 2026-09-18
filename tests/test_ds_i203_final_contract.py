"""Final DS-I203 migration contracts.

The tests protect both the reusable Hikvision bootstrap outcomes and the full
stock-to-OpenIPC NOR install sequence around the published DDR U-Boot variant.
"""

from __future__ import annotations

import io
import json
import sys
import tarfile
from dataclasses import dataclass
from types import ModuleType

import pytest
import typer

from defib.firmware import asset_name
from defib.install import InstallRequest
from defib.transport.base import Transport, TransportTimeout
from defib.uboot_env import select_install_ethaddr
from defib.vendors.hikvision import (
    HIKVISION_DEFAULT_TIMING,
    HikvisionUBootBootstrap,
    HikvisionUBootTiming,
)
from defib.vendors.registry import (
    create_uboot_bootstrap,
    get_stock_uboot_target,
    list_stock_uboot_selectors,
)

FACTORY_MAC = "02:00:00:12:34:56"
SELECTOR = "hi3518ev100:hiwatch-ds-i203"


class ScriptedTransport(Transport):
    """Reactive UART that models the supported Hikvision/OpenIPC entry states."""

    def __init__(self, mode: str, *, mac: str | None = FACTORY_MAC) -> None:
        self.mode = mode
        self.mac = mac
        self.rx = bytearray()
        self.tx: list[bytes] = []
        self.closed = False
        self.break_seen = False
        self.ctrl_u_seen = False
        self.line = bytearray()
        self.flush_input_calls = 0

    def feed(self, data: bytes) -> None:
        self.rx.extend(data)

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if self.closed:
            return b""
        if not self.rx:
            raise TransportTimeout("scripted transport has no data")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def write(self, data: bytes) -> None:
        self.tx.append(bytes(data))
        if data == b"\x03\r":
            self.line.clear()
            if self.mode == "openipc":
                self.feed(b"OpenIPC #")
            elif self.mode in {"stock", "stock-no-mac", "ymodem-fail", "go-fail"}:
                self.feed(b"HKVS #")
        elif data == b"\x03" and self.mode == "cold-stock" and not self.break_seen:
            self.break_seen = True
            self.feed(b"Hit Ctrl+u to stop autoboot:  3")
        elif data == b"\x15" and self.mode == "cold-stock":
            # Hikvision consumes Ctrl+U at the autoboot gate.  The bootstrap
            # deliberately sends it for a short interval; once the gate has
            # opened, repeated interrupt bytes must not become line-editor
            # input for the first stock-shell command.
            if not self.ctrl_u_seen:
                self.ctrl_u_seen = True
                self.feed(b"\r\nHKVS #")
        else:
            for byte in data:
                if byte == 0x03:
                    self.line.clear()
                    continue
                if byte == 0x0D:
                    command = self.line.decode("ascii")
                    self.line.clear()
                    if command == "printenv ethaddr":
                        if self.mac is None:
                            self.feed(b"## Error: ethaddr not defined\r\nHKVS #")
                        else:
                            self.feed(f"ethaddr={self.mac}\r\nHKVS #".encode())
                    elif command.startswith("go ") and self.mode != "go-fail":
                        self.feed(b"\r\nOpenIPC #")
                    continue
                self.line.append(byte)
                # Hikvision's line editor echoes each printable byte.  The
                # bootstrap now verifies this before it sends Enter.
                self.feed(bytes((byte,)))

    async def flush_input(self) -> None:
        self.flush_input_calls += 1
        self.rx.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self.rx)

    async def unread(self, data: bytes) -> None:
        self.rx = bytearray(data) + self.rx

    async def close(self) -> None:
        self.closed = True

    @property
    def all_tx(self) -> bytes:
        return b"".join(self.tx)


@dataclass
class FakeStats:
    bytes_sent: int
    data_packets: int = 1
    retries: int = 0


@pytest.mark.asyncio
async def test_ds_i203_final_migration_contract_all_uboot_outcomes(
    monkeypatch, tmp_path
):
    target = get_stock_uboot_target(SELECTOR)
    assert target is not None

    # The selector chooses the vendor bootstrap and the compatible DDR U-Boot.
    # Camera runtime policy stays outside the release U-Boot, while Defib still
    # owns install-time transport and the flash layout it actually writes.
    assert get_stock_uboot_target("hi3518ev100") is None
    assert "hi3518ev100:hiwatch-ds-i203" in list_stock_uboot_selectors()
    assert target.selector == SELECTOR
    assert target.display_name == "HiWatch DS-I203"
    assert target.vendor == "Hikvision"
    assert target.stock_uboot_name == "Hikvision U-Boot 2010.06"
    assert target.handler == "hikvision"
    assert target.load_address == 0x81000000

    artifact = asset_name(SELECTOR)
    assert artifact == "u-boot-hi3518ev100-ddr3-256m-universal.bin"
    assert target.transient_env == (("phyaddru", "3"),)

    bootstrap = create_uboot_bootstrap(
        target.handler,
        load_address=target.load_address,
    )
    assert isinstance(bootstrap, HikvisionUBootBootstrap)
    assert bootstrap.requires_echo_verification is True
    assert bootstrap.timing == HIKVISION_DEFAULT_TIMING
    assert HIKVISION_DEFAULT_TIMING.boot_timeout == 30.0
    assert HIKVISION_DEFAULT_TIMING.interrupt_duration == 1.0
    assert HIKVISION_DEFAULT_TIMING.openipc_timeout == 15.0

    fast_timing = HIKVISION_DEFAULT_TIMING.scaled(0.001)
    assert isinstance(fast_timing, HikvisionUBootTiming)
    assert fast_timing.ymodem_retries == HIKVISION_DEFAULT_TIMING.ymodem_retries

    def test_bootstrap() -> HikvisionUBootBootstrap:
        return HikvisionUBootBootstrap(
            load_address=target.load_address,
            timing=fast_timing,
        )

    # Exercise the actual install entry point far enough to prove the selector
    # chooses the vendor-U-Boot path without any board-profile subsystem.
    from defib.install import orchestrator

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot_override = tmp_path / artifact
    # Release assets are raw U-Boot binaries: stock chainload uses the raw
    # payload, while the later flash write is padded to the 256 KiB partition.
    uboot_override.write_bytes(b"U" * 182580)

    class ReachedTransport(RuntimeError):
        pass

    async def stop_at_transport(port):
        raise ReachedTransport(port)

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = stop_at_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)
    with pytest.raises(ReachedTransport):
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot_override),
                port="COM15",
                host_ip="192.168.1.11",
                device_ip="192.168.1.64",
                wipe_env=True,
                tftp_via="host",
                output="json",
            )
        )

    from defib.vendors import hikvision

    async def fake_send(self, firmware, *, filename, on_progress=None):
        self._transport.feed(b"\r\nHKVS #")
        if on_progress is not None:
            on_progress(len(firmware), len(firmware))
        return FakeStats(bytes_sent=len(firmware))

    async def fail_send(self, firmware, *, filename, on_progress=None):
        raise hikvision.YModemError("synthetic transfer failure")

    monkeypatch.setattr(hikvision.YModemSender, "send", fake_send)
    firmware = b"release-uboot" * 32

    async def check_existing_openipc() -> None:
        transport = ScriptedTransport("openipc")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.chainloaded is False
        assert result.preserved_env == {}
        assert b"loady " not in transport.all_tx
        assert b"\x03\r" in transport.all_tx

    async def check_warm_stock() -> None:
        transport = ScriptedTransport("stock")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.chainloaded is True
        assert result.preserved_env == {"ethaddr": FACTORY_MAC}
        assert b"printenv ethaddr\r" in transport.all_tx
        assert b"loady 0x81000000\r" in transport.all_tx
        assert b"go 0x81000000\r" in transport.all_tx
        assert b"printenv ethaddr\r" not in transport.tx
        assert b"loady 0x81000000\r" not in transport.tx
        assert b"go 0x81000000\r" not in transport.tx
        assert transport.flush_input_calls >= 1

    async def check_cold_stock() -> None:
        transport = ScriptedTransport("cold-stock")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert transport.ctrl_u_seen is True
        assert b"\x15" in transport.all_tx
        assert result.preserved_env == {"ethaddr": FACTORY_MAC}

    async def check_missing_factory_mac() -> None:
        transport = ScriptedTransport("stock-no-mac", mac=None)
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is True
        assert result.preserved_env == {}
        selected, source = select_install_ethaddr(None, None, allow_generate=False)
        assert selected is None
        assert source == "missing"

    async def check_ymodem_failure() -> None:
        monkeypatch.setattr(hikvision.YModemSender, "send", fail_send)
        transport = ScriptedTransport("ymodem-fail")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is False
        assert "YMODEM failed" in (result.recovery.error or "")
        monkeypatch.setattr(hikvision.YModemSender, "send", fake_send)

    async def check_go_failure() -> None:
        transport = ScriptedTransport("go-fail")
        result = await test_bootstrap().bootstrap(
            transport, firmware, filename=artifact
        )
        assert result.recovery.success is False
        assert result.recovery.error == "OpenIPC U-Boot prompt not detected after go"

    async def check_dead_console() -> None:
        transport = ScriptedTransport("dead")
        with pytest.raises(TimeoutError, match="neither OpenIPC nor Hikvision"):
            await test_bootstrap().bootstrap(
                transport, firmware, filename=artifact
            )

    await check_existing_openipc()
    await check_warm_stock()
    await check_cold_stock()
    await check_missing_factory_mac()
    await check_ymodem_failure()
    await check_go_failure()
    await check_dead_console()


@pytest.mark.asyncio
async def test_ds_i203_stock_install_requires_explicit_env_wipe(monkeypatch):
    from defib.install import orchestrator

    async def unexpected_transport(port: str):
        raise AssertionError(f"transport opened before --wipe-env preflight: {port}")

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = unexpected_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)

    with pytest.raises(typer.Exit) as exc_info:
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path="unused.tgz",
                output="json",
            )
        )

    assert exc_info.value.exit_code == 2


@pytest.mark.asyncio
async def test_install_rejects_oversized_uboot_override_cleanly(
    monkeypatch, tmp_path, capsys
):
    from defib.install import orchestrator

    async def unexpected_transport(port: str):
        raise AssertionError(f"transport opened for oversized U-Boot: {port}")

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = unexpected_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    oversized = tmp_path / "u-boot-too-large.bin"
    oversized.write_bytes(b"U" * (0x40000 + 1))

    with pytest.raises(typer.Exit) as exc_info:
        await orchestrator.run_install(
            InstallRequest(
                chip="hi3518ev100",
                firmware_path=str(firmware_tar),
                uboot_path=str(oversized),
                output="human",
            )
        )

    assert exc_info.value.exit_code == 1
    assert "does not fit the boot partition" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_stock_migration_rejects_download_mode_before_flash(
    monkeypatch, tmp_path
):
    """Vendor migration must never erase flash from an unexpected command mode."""
    import defib.vendors.registry
    from defib.install import orchestrator
    from defib.recovery.events import RecoveryResult
    from defib.vendors.base import UBootBootstrapResult

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot_override = tmp_path / "u-boot.bin"
    uboot_override.write_bytes(b"U" * 4096)

    class DownloadModeTransport(Transport):
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
            if data == b"\x03":
                self.rx.extend(b"start download process")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            pass

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def close(self) -> None:
            self.closed = True

    transport = DownloadModeTransport()

    class FakeBootstrap:
        requires_echo_verification = False

        async def bootstrap(self, uart, firmware, *, filename):
            return UBootBootstrapResult(recovery=RecoveryResult(success=True))

    async def fake_create_transport(port: str):
        return transport

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: FakeBootstrap(),
    )

    with pytest.raises(typer.Exit) as exc_info:
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot_override),
                port="COM15",
                wipe_env=True,
                output="json",
            )
        )

    assert exc_info.value.exit_code == 1
    assert transport.closed is True


@pytest.mark.asyncio
async def test_stock_transport_timeout_is_controlled_and_closes_uart(
    monkeypatch, tmp_path
):
    """TransportTimeout from echo/command IO must not escape as a traceback."""
    import defib.flashdump
    import defib.vendors.registry
    from defib.install import orchestrator
    from defib.recovery.events import RecoveryResult
    from defib.vendors.base import UBootBootstrapResult

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot_override = tmp_path / "u-boot.bin"
    uboot_override.write_bytes(b"U" * 4096)

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
            if data == b"\x03":
                self.rx.extend(b"OpenIPC #")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            pass

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def close(self) -> None:
            self.closed = True

    transport = ShellTransport()

    class FakeBootstrap:
        requires_echo_verification = True

        async def bootstrap(self, uart, firmware, *, filename):
            return UBootBootstrapResult(recovery=RecoveryResult(success=True))

    async def fake_create_transport(port: str):
        return transport

    async def timeout_command(*args, **kwargs):
        raise TransportTimeout("synthetic command timeout")

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)
    monkeypatch.setattr(defib.flashdump, "send_command", timeout_command)
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: FakeBootstrap(),
    )

    with pytest.raises(typer.Exit) as exc_info:
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot_override),
                port="COM15",
                wipe_env=True,
                output="json",
            )
        )

    assert exc_info.value.exit_code == 1
    assert transport.closed is True


@pytest.mark.asyncio
async def test_stock_env_verify_failure_before_tftp_closes_uart(monkeypatch, tmp_path):
    """A bad preserved/transient env readback must still release resources."""
    from defib.install import orchestrator
    from defib.recovery.events import RecoveryResult
    from defib.vendors.base import UBootBootstrapResult
    import defib.vendors.registry

    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", b"kernel"),
            ("rootfs.squashfs.hi3516cv100", b"rootfs"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    uboot_override = tmp_path / "u-boot.bin"
    uboot_override.write_bytes(b"U" * 4096)

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
            if data == b"\x03":
                self.rx.extend(b"OpenIPC #")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            pass

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def close(self) -> None:
            self.closed = True

    transport = ShellTransport()

    class FakeBootstrap:
        requires_echo_verification = False

        async def bootstrap(self, uart, firmware, *, filename):
            return UBootBootstrapResult(
                recovery=RecoveryResult(success=True),
                preserved_env={"ethaddr": FACTORY_MAC},
            )

    async def fake_create_transport(port: str):
        return transport

    async def fail_env_verify(*args, **kwargs):
        raise RuntimeError("synthetic env verify failure")

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: FakeBootstrap(),
    )
    monkeypatch.setattr(orchestrator, "set_uboot_env_verified", fail_env_verify)

    with pytest.raises(typer.Exit) as exc_info:
        await orchestrator.run_install(
            InstallRequest(
                chip=SELECTOR,
                firmware_path=str(firmware_tar),
                uboot_path=str(uboot_override),
                port="COM15",
                wipe_env=True,
                output="json",
            )
        )

    assert exc_info.value.exit_code == 1
    assert transport.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crc_failure",
    [None, "tftp", "tftp-once", "readback", "env"],
)
async def test_ds_i203_stock_install_persists_detected_layout_but_not_camera_policy(
    monkeypatch, tmp_path, crc_failure, capsys
):
    """Exercise the complete stock->OpenIPC NOR install contract.

    The published DDR variant is a raw U-Boot with generic environment defaults.
    Defib must pad it, use PHY3 only transiently for its own TFTP session, flash
    the detected 16 MiB layout, erase the old persistent environment so the new
    U-Boot loads its compiled defaults, and persist the layout it actually wrote
    plus the factory MAC. Camera policy such as osmem,
    Linux extras and sensor selection must remain outside Defib.
    """
    import zlib
    from contextlib import asynccontextmanager

    from defib.install import orchestrator
    from defib.install.layout import nor_mtdparts

    assert orchestrator.TFTP_RAM_VERIFY_RETRIES == 1
    from defib.recovery.events import RecoveryResult
    from defib.vendors.base import UBootBootstrapResult

    artifact = asset_name(SELECTOR)
    assert artifact == "u-boot-hi3518ev100-ddr3-256m-universal.bin"

    kernel = b"K" * 0x18000
    rootfs = b"R" * 0x28000
    firmware_tar = tmp_path / "openipc.hi3516cv100-nor-lite.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516cv100", kernel),
            ("rootfs.squashfs.hi3516cv100", rootfs),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    raw_uboot = b"U" * 182580
    uboot_override = tmp_path / artifact
    uboot_override.write_bytes(raw_uboot)

    class InstallTransport(Transport):
        def __init__(self) -> None:
            self.rx = bytearray()
            self.closed = False

        async def read(self, size: int, timeout: float | None = None) -> bytes:
            if not self.rx:
                raise TransportTimeout("no scripted UART data")
            data = bytes(self.rx[:size])
            del self.rx[:size]
            return data

        async def write(self, data: bytes) -> None:
            if data == b"\x03":
                self.rx.extend(b"OpenIPC #")

        async def flush_input(self) -> None:
            self.rx.clear()

        async def flush_output(self) -> None:
            pass

        async def bytes_waiting(self) -> int:
            return len(self.rx)

        async def unread(self, data: bytes) -> None:
            self.rx = bytearray(data) + self.rx

        async def close(self) -> None:
            self.closed = True

    transport = InstallTransport()

    class FakeBootstrap:
        requires_echo_verification = False

        async def bootstrap(self, uart, firmware, *, filename):
            assert uart is transport
            assert filename == artifact
            # Chainload the raw release artifact; only the flash write needs
            # the 0xFF-padded fixed-size boot partition image.
            assert firmware == raw_uboot
            return UBootBootstrapResult(
                recovery=RecoveryResult(success=True),
                preserved_env={"ethaddr": FACTORY_MAC},
            )

    async def fake_create_transport(port: str):
        assert port == "COM15"
        return transport

    @asynccontextmanager
    async def fake_temporary_ip(interface: str, ip: str, netmask: str):
        assert interface == "Ethernet"
        assert ip == "192.168.1.11"
        yield

    class FakeTFTPTransport:
        def close(self) -> None:
            pass

    class FakeTFTPProtocol:
        def __init__(self, files):
            self._files = dict(files)
            self.blocksize_caps: list[int] = []

        def set_max_blocksize(self, blocksize: int) -> None:
            self.blocksize_caps.append(blocksize)

    tftp_files: dict[str, bytes] = {}
    tftp_protocol_obj: FakeTFTPProtocol | None = None

    async def fake_start_tftp_server(*, files, bind_addr, port, done_count):
        nonlocal tftp_protocol_obj
        assert bind_addr == "192.168.1.11"
        assert done_count == 3
        tftp_files.clear()
        tftp_files.update(files)
        tftp_protocol_obj = FakeTFTPProtocol(files)
        return FakeTFTPTransport(), tftp_protocol_obj

    env: dict[str, str] = {"ethaddr": FACTORY_MAC}
    saved_env: dict[str, str] = {}
    commands: list[str] = []
    flash = bytearray(b"\xa5" * 0x1000000)
    ram = b""
    stored_crc_word: int | None = None
    env_erased = False
    crc_calls = 0

    generic_defaults = {
        "osmem": "32M",
        "mtdparts": "hi_sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)",
        "bootcmd": "${bootcmdnor}",
    }

    async def fake_send_command(
        uart, command: str, timeout: float = 60.0, **kwargs
    ) -> str:
        nonlocal ram, env_erased, saved_env, env, crc_calls, stored_crc_word
        assert uart is transport
        commands.append(command)

        if command == "sf probe 0":
            return 'Spi(cs1): Block:64KB Chip:16MB Name:"GD25Q128"\nOpenIPC # '

        if command.startswith("setenv "):
            parts = command.split(" ", 2)
            key = parts[1]
            if len(parts) == 2 or parts[2] == "":
                env.pop(key, None)
            else:
                env[key] = parts[2]
            return "OpenIPC # "

        if command.startswith("printenv "):
            key = command.split(" ", 1)[1]
            if key in env:
                return f"{key}={env[key]}\nOpenIPC # "
            return f"## Error: {key} not defined\nOpenIPC # "

        if command.startswith(("tftpboot ", "tftp ")):
            name = command.split()[-1]
            ram = tftp_files[name]
            return f"Bytes transferred = {len(ram)}\nOpenIPC # "

        if command.startswith("sf erase "):
            _, _, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            flash[off:off + size] = b"\xff" * size
            if off == 0x40000 and size == 0x10000:
                env_erased = True
            return "Erasing at 0x0 -- 100% complete.\nOpenIPC # "

        if command.startswith("sf write "):
            _, _, _addr, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            flash[off:off + size] = ram[:size]
            return "Writing at 0x0 -- 100% complete.\nOpenIPC # "

        if command.startswith("sf read ") and ";" not in command:
            _, _, _addr, off_text, size_text = command.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            ram = bytes(flash[off:off + size])
            return "Read OK\nOpenIPC # "

        if command.startswith("sf read ") and "; crc32 " in command:
            read_part, crc_part = command.split(";", 1)
            _, _, _addr, off_text, size_text = read_part.split()
            off = int(off_text, 16)
            size = int(size_text, 16)
            ram = bytes(flash[off:off + size])
            crc_size = int(crc_part.split()[-1], 16)
            crc = zlib.crc32(ram[:crc_size]) & 0xFFFFFFFF
            return f"==> {crc:08X}\nOpenIPC # "

        if command.startswith("crc32 "):
            crc_calls += 1
            if crc_failure == "tftp" and crc_calls <= 2:
                return "==> 00000000\nOpenIPC # "
            if crc_failure == "tftp-once" and crc_calls == 1:
                return "==> 00000000\nOpenIPC # "
            if crc_failure == "readback" and crc_calls == 2:
                return "CRC32 output truncated\nOpenIPC # "
            parts = command.split()
            address = int(parts[1], 16)
            size = int(parts[2], 16)
            offset = address - 0x82000000
            crc = zlib.crc32(ram[offset:offset + size]) & 0xFFFFFFFF
            if len(parts) == 4:
                stored_crc_word = crc
            return f"==> {crc:08X}\nOpenIPC # "

        if command.startswith("cmp.l "):
            header_crc = int.from_bytes(ram[:4], "little")
            if stored_crc_word == header_crc:
                return "Total of 1 word(s) were the same\nOpenIPC # "
            return (
                f"word at 0x82000000 ({header_crc:08x}) != "
                f"({(stored_crc_word or 0):08x})\nOpenIPC # "
            )

        if command == "saveenv":
            saved_env = dict(env)
            env_data = b"\x00".join(
                f"{key}={value}".encode("ascii")
                for key, value in sorted(env.items())
            ) + b"\x00\x00"
            env_data = env_data.ljust(0x10000 - 4, b"\x00")
            env_crc = zlib.crc32(env_data) & 0xFFFFFFFF
            flash[0x40000:0x50000] = env_crc.to_bytes(4, "little") + env_data
            if crc_failure == "env":
                # Simulate a short/corrupt SPI persistence after saveenv printed
                # success.  RAM printenv still looks correct, but the on-flash
                # environment CRC must reject the install.
                flash[0x40004] ^= 0x01
            return "Saving Environment to SPI Flash... done\nOpenIPC # "

        if command == "reset":
            if env_erased:
                env = dict(generic_defaults)
                env_erased = False
            return "resetting..."

        return "OpenIPC # "

    import defib.flashdump
    import defib.network.ip_manager
    import defib.network.tftp_server
    import defib.vendors.registry

    serial_platform = ModuleType("defib.transport.serial_platform")
    serial_platform.create_transport = fake_create_transport
    serial_platform.normalize_port_name = lambda port: port
    monkeypatch.setitem(sys.modules, "defib.transport.serial_platform", serial_platform)

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)
    monkeypatch.setattr(defib.network.ip_manager, "temporary_ip", fake_temporary_ip)
    monkeypatch.setattr(
        defib.network.tftp_server, "start_tftp_server", fake_start_tftp_server
    )
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: FakeBootstrap(),
    )

    request = InstallRequest(
        chip=SELECTOR,
        firmware_path=str(firmware_tar),
        uboot_path=str(uboot_override),
        port="COM15",
        nic="Ethernet",
        host_ip="192.168.1.11",
        device_ip="192.168.1.64",
        wipe_env=True,
        tftp_via="host",
        output="json",
    )

    if crc_failure in (None, "tftp-once"):
        await orchestrator.run_install(request)
    else:
        with pytest.raises(typer.Exit) as exc_info:
            await orchestrator.run_install(request)
        assert exc_info.value.exit_code == 1
        if crc_failure == "tftp":
            assert not any(cmd.startswith("sf erase ") for cmd in commands)
            assert not any(cmd.startswith("sf write ") for cmd in commands)
        elif crc_failure == "readback":
            assert any(cmd.startswith("sf write ") for cmd in commands)
            assert "tftpboot k" not in commands
        else:
            assert "saveenv" in commands
            assert "sf read 0x82000000 0x40000 0x10000" in commands
            assert any(cmd.startswith("cmp.l 0x82000000 ") for cmd in commands)
        return

    if crc_failure == "tftp-once":
        assert commands.count("tftpboot u") == 2
        assert tftp_protocol_obj is not None
        assert tftp_protocol_obj.blocksize_caps == [512]

        warning_lines = [
            line
            for line in capsys.readouterr().out.splitlines()
            if '"event": "warning"' in line
        ]
        assert len(warning_lines) == 1
        warning = json.loads(warning_lines[0])
        assert warning["message"].startswith(
            "Attempt 2: fetching TFTP file 'u' again for U-Boot "
        )
        assert "CRC expected=" in warning["message"]
        assert "using 512-byte blocks." in warning["message"]

        uboot_tftp = [
            index
            for index, command in enumerate(commands)
            if command == "tftpboot u"
        ]
        uboot_crc = [
            index
            for index, command in enumerate(commands)
            if command.startswith("crc32 0x82000000 0x40000")
        ]
        first_erase = next(
            index
            for index, command in enumerate(commands)
            if command.startswith("sf erase ")
        )
        assert len(uboot_tftp) == 2
        assert len(uboot_crc) >= 2
        assert (
            uboot_tftp[0]
            < uboot_crc[0]
            < uboot_tftp[1]
            < uboot_crc[1]
            < first_erase
        )

    expected_mtdparts = nor_mtdparts(16)

    # Installer-only PHY override must be established before the first TFTP.
    assert commands.index("setenv phyaddru 3") < commands.index("tftpboot u")
    assert "printenv phyaddru" in commands

    # The standard 16 MiB offsets selected from sf probe are what was written.
    assert flash[: len(raw_uboot)] == raw_uboot
    assert flash[len(raw_uboot):0x40000] == b"\xff" * (0x40000 - len(raw_uboot))
    assert flash[0x50000:0x50000 + len(kernel)] == kernel
    assert flash[0x350000:0x350000 + len(rootfs)] == rootfs
    assert flash[0xD50000:] == b"\xff" * (0x1000000 - 0xD50000)

    # Erasing the persistent env makes the freshly flashed U-Boot materialize
    # its generic compiled defaults. Defib then persists only the layout it
    # actually flashed and the factory identity; device policy stays generic
    # until the firmware profile customizer applies it in Linux.
    assert saved_env["ethaddr"] == FACTORY_MAC
    assert saved_env["mtdparts"] == expected_mtdparts
    assert saved_env["osmem"] == "32M"
    assert "phyaddru" not in saved_env
    assert "extras" not in saved_env
    assert "sensor" not in saved_env

    assert "sf erase 0x40000 0x10000" in commands
    assert any(
        cmd.startswith("sf read ") and " 0x40000 0x10000; crc32 " in cmd
        for cmd in commands
    )
    assert "setenv restore y" not in commands
    assert f"setenv mtdparts {expected_mtdparts}" in commands
    assert "printenv mtdparts" in commands
    assert not any(cmd.startswith("setenv osmem ") for cmd in commands)
    assert not any(cmd.startswith("setenv extras ") for cmd in commands)
    assert not any(cmd.startswith("setenv sensor ") for cmd in commands)
