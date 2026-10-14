"""`defib restore` must stage TFTP loads in the chip's DRAM.

Regression: restore hardcoded ram_addr = 0x82000000. On parts whose DRAM
starts at 0x40000000 (gk7201v200, gk7205v200, hi3516ev300, ...) that sends
the TFTP load and every flash write to an address with no memory behind it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import defib.network.tftp_server
import defib.recovery.session
import defib.transport.serial_platform
from defib.cli.app import app


class FakeUBoot:
    """A U-Boot console that answers the commands restore sends."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self._rx = bytearray()

    async def write(self, data: bytes) -> None:
        if data == b"\x03":
            self._rx.extend(b"OpenIPC # ")
            return
        cmd = data.decode("ascii", errors="replace").strip()
        self.commands.append(cmd)
        if cmd.startswith("ping"):
            reply = "host 10.0.0.1 is alive\n"
        elif cmd.startswith(("tftpboot", "tftp ")):
            reply = "Bytes transferred = 4096 (1000 hex)\ndone\n"
        else:
            reply = ""
        self._rx.extend((reply + "OpenIPC # ").encode())

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data

    async def bytes_waiting(self) -> int:
        return len(self._rx)

    async def flush_input(self) -> None:
        self._rx.clear()

    async def flush_output(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.fixture
def uboot(monkeypatch: pytest.MonkeyPatch) -> FakeUBoot:
    console = FakeUBoot()

    async def fake_create_transport(port: str) -> FakeUBoot:
        return console

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self, transport: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(success=True, elapsed_ms=1.0, error=None)

    async def fake_tftp_server(**kwargs: object) -> tuple[SimpleNamespace, SimpleNamespace]:
        return SimpleNamespace(close=lambda: None), SimpleNamespace(_files={})

    monkeypatch.setattr(defib.transport.serial_platform, "create_transport", fake_create_transport)
    monkeypatch.setattr(defib.recovery.session, "RecoverySession", FakeSession)
    monkeypatch.setattr(defib.network.tftp_server, "start_tftp_server", fake_tftp_server)
    return console


def _restore(tmp_path: Path, chip: str) -> None:
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "mtd0").write_bytes(b"\x15\x05\x00\xea" * 1024)
    (dump / "mtd1").write_bytes(b"\xab" * 4096)
    uboot_bin = tmp_path / "u-boot.bin"
    uboot_bin.write_bytes(b"\x00" * 64)

    result = CliRunner().invoke(app, [
        "restore", "-c", chip, "-i", str(dump), "-p", "/dev/null",
        "--uboot", str(uboot_bin), "--flash-type", "nor",
        "--host-ip", "10.0.0.1", "--device-ip", "10.0.0.2",
    ])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("chip", "staging"),
    [("gk7201v200", 0x42000000), ("hi3516ev300", 0x42000000), ("hi3516cv300", 0x82000000)],
)
def test_restore_stages_in_chip_dram(
    tmp_path: Path, uboot: FakeUBoot, chip: str, staging: int,
) -> None:
    _restore(tmp_path, chip)

    loads = [c for c in uboot.commands if c.startswith("tftpboot")]
    writes = [c for c in uboot.commands if c.startswith("sf write")]
    assert [c.split()[1] for c in loads] == [f"0x{staging:x}"] * 2
    assert [c.split()[2] for c in writes] == [f"0x{staging:x}"] * 2
    # Boot partition still goes last.
    assert writes[-1].split()[3] == "0x0"
