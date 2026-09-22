from __future__ import annotations

import io
import tarfile

import pytest
import typer

from defib.install import InstallRequest
from defib.install.layout import erased_region_crc
from defib.recovery.events import RecoveryResult
from defib.transport.base import Transport, TransportTimeout


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


async def _prepare_generic_install(monkeypatch, tmp_path):
    import defib.flashdump
    import defib.recovery.session
    import defib.transport.serial_platform
    from defib.install.orchestrator import run_install

    firmware_tar = tmp_path / "firmware.tgz"
    with tarfile.open(firmware_tar, "w:gz") as archive:
        for name, payload in (
            ("uImage.hi3516ev200", b"K" * 1024),
            ("rootfs.squashfs.hi3516ev200", b"R" * 2048),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    uboot = tmp_path / "u-boot.bin"
    uboot.write_bytes(b"U" * 1024)

    transport = ShellTransport()
    commands: list[str] = []
    expected_crc = erased_region_crc(0xB0000)

    class FakeRecoverySession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, transport_obj, **kwargs):
            assert transport_obj is transport
            return RecoveryResult(success=True)

    async def fake_create_transport(port: str):
        assert port == "COM15"
        return transport

    async def fake_send_command(
        transport_obj, command: str, timeout: float = 0.0, **kwargs
    ) -> str:
        assert transport_obj is transport
        commands.append(command)
        if command == "sf probe 0":
            return 'Spi(cs1): Block:64KB Chip:8MB Name:"XT25F64B"\nOpenIPC # '
        if command == "sf lock 0":
            return "OpenIPC # "
        if command == "sf erase 0x750000 0xb0000":
            return "Erasing at 0x800000 -- 100% complete.\nOpenIPC # "
        if command == (
            "sf read 0x42000000 0x750000 0xb0000; "
            "crc32 0x42000000 0xb0000"
        ):
            return f"==> {expected_crc:08X}\nOpenIPC # "
        if command == "reset":
            return "resetting..."
        return "OpenIPC # "

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)
    monkeypatch.setattr(
        defib.recovery.session,
        "RecoverySession",
        FakeRecoverySession,
    )
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

    def request(**kwargs) -> InstallRequest:
        return InstallRequest(
            chip="hi3516ev200",
            firmware_path=str(firmware_tar),
            uboot_path=str(uboot),
            port="COM15",
            nor_size=8,
            output="json",
            **kwargs,
        )

    return run_install, request, commands, transport


@pytest.mark.asyncio
async def test_generic_rootfs_data_stage_keeps_existing_noop_behavior(
    monkeypatch, tmp_path
):
    run_install, request, commands, transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    await run_install(request(stages=("rootfs-data",)))

    assert "sf erase 0x750000 0xb0000" not in commands
    assert not any(" 0x750000 0xb0000" in command for command in commands)
    assert transport.closed is True


@pytest.mark.asyncio
async def test_wipe_rootfs_data_flag_erases_and_verifies_in_exact_stage_plan(
    monkeypatch, tmp_path
):
    run_install, request, commands, transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    await run_install(
        request(
            stages=("rootfs-data", "reset"),
            wipe_rootfs_data=True,
        )
    )

    unlock = "sf lock 0"
    erase = "sf erase 0x750000 0xb0000"
    verify = "sf read 0x42000000 0x750000 0xb0000; crc32 0x42000000 0xb0000"
    assert unlock in commands
    assert erase in commands
    assert verify in commands
    assert commands.index(unlock) < commands.index(erase) < commands.index(verify)
    assert commands.index(verify) < commands.index("reset")
    assert transport.closed is True


@pytest.mark.asyncio
async def test_wipe_rootfs_data_rejects_nand_before_transport(monkeypatch, tmp_path):
    run_install, request, commands, _transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(
            request(
                stages=("rootfs-data", "reset"),
                wipe_rootfs_data=True,
                nand=True,
            )
        )

    assert exc_info.value.exit_code == 2
    assert commands == []


@pytest.mark.asyncio
async def test_wipe_rootfs_data_conflicts_with_skip_stage(monkeypatch, tmp_path):
    run_install, request, commands, _transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(
            request(
                skip_stages=("rootfs-data",),
                wipe_rootfs_data=True,
            )
        )

    assert exc_info.value.exit_code == 2
    assert commands == []


@pytest.mark.asyncio
async def test_wipe_rootfs_data_conflicts_with_exact_stage_omitting_rootfs_data(
    monkeypatch, tmp_path
):
    run_install, request, commands, _transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(
            request(
                stages=("uboot",),
                wipe_rootfs_data=True,
            )
        )

    assert exc_info.value.exit_code == 2
    assert commands == []


@pytest.mark.asyncio
async def test_chainloaded_stock_uboot_wipe_requires_uboot_stage(monkeypatch, tmp_path):
    import defib.vendors.registry
    from defib.vendors.base import UBootBootstrapResult
    from defib.vendors.registry import StockUBootTarget

    run_install, request, commands, transport = await _prepare_generic_install(
        monkeypatch, tmp_path
    )

    target = StockUBootTarget(
        selector="hi3516ev200:test-stock",
        handler="synthetic",
        load_address=0x82000000,
        display_name="Synthetic stock board",
        vendor="Test",
        stock_uboot_name="Synthetic U-Boot",
    )

    class ChainloadedBootstrap:
        requires_echo_verification = False

        async def bootstrap(self, transport_obj, firmware, *, filename):
            assert transport_obj is transport
            return UBootBootstrapResult(
                recovery=RecoveryResult(success=True),
                chainloaded=True,
            )

    monkeypatch.setattr(
        defib.vendors.registry,
        "get_stock_uboot_target",
        lambda selector: target,
    )
    monkeypatch.setattr(
        defib.vendors.registry,
        "create_uboot_bootstrap",
        lambda *args, **kwargs: ChainloadedBootstrap(),
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(
            request(
                stages=("rootfs-data",),
                wipe_rootfs_data=True,
            )
        )

    assert exc_info.value.exit_code == 2
    assert not any(command.startswith("sf erase ") for command in commands)
    assert transport.closed is True
