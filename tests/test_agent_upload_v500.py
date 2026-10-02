"""Tests for the V500 agent-upload path's failure handling."""

from __future__ import annotations

import asyncio
import io
import struct
from pathlib import Path
from typing import Any

import pytest
import typer
from rich.console import Console

from defib.cli import app as cli_app
from defib.power.base import PowerController
from defib.protocol.hisilicon_v500 import HiSiliconV500, v500_member
from defib.recovery.events import HandshakeResult
from defib.transport.mock import MockTransport


def _write_donor(tmp_path: Path) -> Path:
    image = bytearray(b"\xa5" * (0x7000 + 0x800 + 0x200))
    struct.pack_into("<3I", image, 0x400, 0x5000, 0x800, 0xa00)
    path = tmp_path / "donor.bin"
    path.write_bytes(bytes(image))
    return path


class ClosingPower(PowerController):
    def __init__(self) -> None:
        self.closed = False

    @classmethod
    def name(cls) -> str:
        return "test"

    async def power_off(self, port: str) -> None:
        return None

    async def power_on(self, port: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


async def _run(
    tmp_path: Path, chip: str, *, donor: bool = False, power_cycle: bool = False,
) -> str:
    out = io.StringIO()
    with pytest.raises(typer.Exit):
        await cli_app._agent_upload_v500(
            chip=chip, port="/dev/null", output="human",
            console=Console(file=out, width=200),
            agent_path=tmp_path / "agent.bin", agent_data=b"\x00" * 64,
            donor_path=str(_write_donor(tmp_path)) if donor else None,
            power_cycle=power_cycle,
        )
    return out.getvalue()


@pytest.fixture
def fake_link(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MockTransport:
    transport = MockTransport()

    async def fake_create(_url: str) -> MockTransport:
        return transport

    monkeypatch.setattr("defib.transport.serial_platform.create_transport", fake_create)
    monkeypatch.setattr("defib.firmware.download_v500_donor",
                        lambda chip: _write_donor(tmp_path))
    return transport


def test_v500_member() -> None:
    assert v500_member("gk7205v510") == "v510"
    assert v500_member("XM7205V530:board") == "v530"
    assert v500_member("hi3516ev300") is None


async def test_wrong_chip_is_named_before_upload(
    fake_link: MockTransport, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def handshake(self: HiSiliconV500, transport: Any, cb: Any = None) -> HandshakeResult:
        return HandshakeResult(success=True, chip_id=0x72050510)

    monkeypatch.setattr(HiSiliconV500, "handshake", handshake)
    text = await _run(tmp_path, "gk7205v500")
    assert "V510" in text and "-c gk7205v510" in text
    # Nothing beyond the handshake went out: no HEAD frame.
    assert b"\xfe\x00\xff\x01" not in fake_link.all_tx_data


async def test_explicit_donor_skips_the_chip_check(
    fake_link: MockTransport, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def handshake(self: HiSiliconV500, transport: Any, cb: Any = None) -> HandshakeResult:
        return HandshakeResult(success=True, chip_id=0x72050510)

    async def send(self: HiSiliconV500, *a: Any, **k: Any) -> Any:
        from defib.recovery.events import RecoveryResult
        return RecoveryResult(success=False, error="stop here")

    monkeypatch.setattr(HiSiliconV500, "handshake", handshake)
    monkeypatch.setattr(HiSiliconV500, "send_firmware", send)
    text = await _run(tmp_path, "gk7205v500", donor=True)
    assert "stop here" in text


async def test_manual_handshake_times_out(
    fake_link: MockTransport, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def handshake(self: HiSiliconV500, transport: Any, cb: Any = None) -> HandshakeResult:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(HiSiliconV500, "handshake", handshake)
    monkeypatch.setattr(cli_app, "MANUAL_HANDSHAKE_TIMEOUT", 0.05)
    text = await _run(tmp_path, "gk7205v510")
    assert "No bootrom response" in text


async def test_power_closed_when_port_cannot_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    power = ClosingPower()

    async def broken_create(_url: str) -> MockTransport:
        raise OSError("no such device")

    monkeypatch.setattr("defib.transport.serial_platform.create_transport", broken_create)
    monkeypatch.setattr("defib.power.factory.power_controller_from_env", lambda: power)
    text = await _run(tmp_path, "gk7205v510", donor=True, power_cycle=True)
    assert "no such device" in text
    assert power.closed


def test_donor_name_is_lowercase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from defib import firmware

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    urls: list[str] = []

    def fake_download(url: str, dest: Path, on_progress: Any = None) -> Path:
        urls.append(url)
        return dest

    monkeypatch.setattr(firmware, "_download", fake_download)
    firmware.download_v500_donor("GK7205V510")
    assert urls[0].endswith("/u-boot-gk7205v510-nor.bin")
