from __future__ import annotations

import io
import tarfile

import pytest
import typer

from defib.install import InstallRequest
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


async def _run_env_only_install(
    monkeypatch,
    tmp_path,
    unlock_response: str,
    *,
    probe_response: str = 'Spi(cs1): Block:64KB Chip:8MB Name:"XT25F64B"\nOpenIPC # ',
    download_mode: bool = False,
    download_unlock_ok: bool = True,
):
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

    class DownloadModeTransport(ShellTransport):
        async def write(self, data: bytes) -> None:
            if b"\x03" in data:
                self.rx.extend(b"start download process.\n")

    transport = DownloadModeTransport() if download_mode else ShellTransport()
    commands: list[str] = []

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
            return probe_response
        if command == "sf lock 0":
            return unlock_response
        if command == "printenv ethaddr":
            return "ethaddr=00:12:41:8e:c6:0e\nOpenIPC # "
        if command == "saveenv":
            return "Saving Environment to SPI Flash... done\nOpenIPC # "
        return "OpenIPC # "

    monkeypatch.setattr(defib.flashdump, "send_command", fake_send_command)

    import defib.protocol.download_cmd

    class FakeDownloadCommandClient:
        def __init__(self, transport_obj) -> None:
            assert transport_obj is transport

        async def send_command(
            self, command: str, timeout: float = 0.0
        ) -> tuple[bool, str]:
            commands.append(command)
            if command == "sf probe 0":
                return True, probe_response
            if command == "sf lock 0":
                return download_unlock_ok, unlock_response
            if command == "printenv ethaddr":
                return True, "ethaddr=00:12:41:8e:c6:0e\n"
            if command == "saveenv":
                return True, "Saving Environment to SPI Flash... done\n"
            return True, ""

    monkeypatch.setattr(
        defib.protocol.download_cmd,
        "DownloadCommandClient",
        FakeDownloadCommandClient,
    )
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

    request = InstallRequest(
        chip="hi3516ev200",
        firmware_path=str(firmware_tar),
        uboot_path=str(uboot),
        port="COM15",
        nor_size=8,
        stages=("env",),
        output="json",
    )
    return run_install, request, commands, transport


@pytest.mark.asyncio
async def test_nor_unlock_runs_before_persistent_env_write(monkeypatch, tmp_path):
    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "OpenIPC # ",
    )

    await run_install(request)

    assert commands.index("sf probe 0") < commands.index("sf lock 0")
    assert commands.index("sf lock 0") < commands.index("saveenv")
    assert transport.closed is True


@pytest.mark.asyncio
async def test_nor_unlock_unsupported_is_compatible(monkeypatch, tmp_path):
    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "Usage:\nsf probe [[bus:]cs] [hz] [mode]\nsf read addr offset len\nOpenIPC # ",
    )

    await run_install(request)

    assert "sf lock 0" in commands
    assert "saveenv" in commands
    assert transport.closed is True


@pytest.mark.asyncio
async def test_nor_unlock_failure_stops_before_persistent_write(monkeypatch, tmp_path):
    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "ERROR: SPI NOR unlock failed\nOpenIPC # ",
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(request)

    assert exc_info.value.exit_code == 1
    assert "sf lock 0" in commands
    assert "saveenv" not in commands
    assert transport.closed is True


@pytest.mark.asyncio
async def test_nor_probe_failure_stops_before_unlock_with_size_override(
    monkeypatch, tmp_path
):
    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "OpenIPC # ",
        probe_response="No SPI flash selected. Please run `sf probe'\nOpenIPC # ",
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(request)

    assert exc_info.value.exit_code == 1
    assert commands == ["sf probe 0"]
    assert transport.closed is True


@pytest.mark.asyncio
async def test_download_command_failure_stops_before_persistent_write(
    monkeypatch, tmp_path
):
    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "",
        download_mode=True,
        download_unlock_ok=False,
    )

    with pytest.raises(typer.Exit) as exc_info:
        await run_install(request)

    assert exc_info.value.exit_code == 1
    assert "sf probe 0" in commands
    assert "sf lock 0" in commands
    assert "saveenv" not in commands
    assert transport.closed is True



@pytest.mark.asyncio
async def test_final_reset_does_not_require_prompt(monkeypatch, tmp_path):
    from dataclasses import replace

    import defib.flashdump

    run_install, request, commands, transport = await _run_env_only_install(
        monkeypatch,
        tmp_path,
        "OpenIPC # ",
    )

    async def prompt_sensitive_send_command(
        transport_obj,
        command: str,
        timeout: float = 0.0,
        wait_for: str | None = None,
        **kwargs,
    ) -> str:
        assert transport_obj is transport
        commands.append(command)
        if command == "sf probe 0":
            return 'Spi(cs1): Block:64KB Chip:8MB Name:"XT25F64B"\nOpenIPC # '
        if command == "reset":
            assert wait_for is None
            return "resetting...\n"
        return "OpenIPC # "

    monkeypatch.setattr(
        defib.flashdump,
        "send_command",
        prompt_sensitive_send_command,
    )
    reset_request = replace(request, stages=("reset",))

    await run_install(reset_request)

    assert commands == ["sf probe 0", "reset"]
    assert transport.closed is True
