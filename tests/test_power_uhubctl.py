"""Tests for the uhubctl USB hub port power controller."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from defib.power import uhubctl as uhubctl_mod
from defib.power.base import PowerControllerError
from defib.power.factory import power_controller_from_env
from defib.power.uhubctl import UhubctlController, parse_new_status

# Real uhubctl 2.6.0 output for a TerraMaster USB 3 hub (USB 2 twin 3-2,
# USB 3 twin 4-2): both twins are switched and reported.
OFF_OUTPUT = """\
Current status for hub 4-2 [0bda:0423 TerraMaster 4-Port USB 3.0 Hub, USB 3.20, 4 ports, ppps]
  Port 1: 02a0 power 5gbps Rx.Detect
Sent power off request
New status for hub 4-2 [0bda:0423 TerraMaster 4-Port USB 3.0 Hub, USB 3.20, 4 ports, ppps]
  Port 1: 00a0 off
Current status for hub 3-2 [0bda:5423 TerraMaster 4-Port USB 2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0100 power
Sent power off request
New status for hub 3-2 [0bda:5423 TerraMaster 4-Port USB 2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0000 off
"""

ON_OUTPUT = """\
Current status for hub 4-2 [0bda:0423 TerraMaster 4-Port USB 3.0 Hub, USB 3.20, 4 ports, ppps]
  Port 1: 00a0 off
Sent power on request
New status for hub 4-2 [0bda:0423 TerraMaster 4-Port USB 3.0 Hub, USB 3.20, 4 ports, ppps]
  Port 1: 02a0 power 5gbps Rx.Detect
Current status for hub 3-2 [0bda:5423 TerraMaster 4-Port USB 2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0000 off
Sent power on request
New status for hub 3-2 [0bda:5423 TerraMaster 4-Port USB 2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0100 power
"""

NOT_FOUND_OUTPUT = """\
No compatible devices detected at location 9-9!
Run with -h to get usage info.
"""


class FakeProc:
    def __init__(self, output: str, returncode: int) -> None:
        self._output = output.encode()
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, None]:
        return self._output, None

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode


class FakeUhubctl:
    """Stands in for create_subprocess_exec, answering by the ``-a`` action."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.outputs = {"off": OFF_OUTPUT, "on": ON_OUTPUT}
        self.returncode = 0

    async def __call__(self, *argv: str, **kwargs: Any) -> FakeProc:
        self.calls.append(argv)
        action = argv[argv.index("-a") + 1]
        return FakeProc(self.outputs[action], self.returncode)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeUhubctl:
    f = FakeUhubctl()
    monkeypatch.setattr(uhubctl_mod.asyncio, "create_subprocess_exec", f)
    return f


def _ctl(**kw: Any) -> UhubctlController:
    return UhubctlController(location="3-2", port=1, **kw)


class TestParse:
    def test_reads_both_twins_new_status_only(self) -> None:
        got = parse_new_status(OFF_OUTPUT, 1)
        assert [(hub, flags) for hub, flags, _ in got] == [
            ("4-2", "off"), ("3-2", "off"),
        ]

    def test_other_port_ignored(self) -> None:
        assert parse_new_status(OFF_OUTPUT, 2) == []


class TestSwitch:
    def test_off_then_on(self, fake: FakeUhubctl) -> None:
        ctl = _ctl()
        asyncio.run(ctl.power_off(""))
        asyncio.run(ctl.power_on(""))
        assert fake.calls == [
            ("uhubctl", "-l", "3-2", "-p", "1", "-a", "off"),
            ("uhubctl", "-l", "3-2", "-p", "1", "-a", "on"),
        ]

    def test_power_cycle(self, fake: FakeUhubctl) -> None:
        asyncio.run(_ctl().power_cycle("", off_duration=0))
        assert [c[-1] for c in fake.calls] == ["off", "on"]

    def test_custom_command(self, fake: FakeUhubctl) -> None:
        asyncio.run(_ctl(command=["sudo", "/usr/local/bin/uhubctl"]).power_off(""))
        assert fake.calls[0][:2] == ("sudo", "/usr/local/bin/uhubctl")

    def test_port_still_powered_is_error(self, fake: FakeUhubctl) -> None:
        # One twin switched, the other did not.
        fake.outputs["off"] = OFF_OUTPUT.replace("0000 off", "0100 power")
        with pytest.raises(PowerControllerError, match="hub 3-2 reports 'power'"):
            asyncio.run(_ctl().power_off(""))

    def test_no_status_is_error(self, fake: FakeUhubctl) -> None:
        fake.outputs["off"] = "Sent power off request\n"
        with pytest.raises(PowerControllerError, match="no new port status"):
            asyncio.run(_ctl().power_off(""))

    def test_nonzero_exit_is_error(self, fake: FakeUhubctl) -> None:
        fake.outputs["off"] = NOT_FOUND_OUTPUT
        fake.returncode = 1
        with pytest.raises(PowerControllerError, match="exited 1.*No compatible"):
            asyncio.run(_ctl().power_off(""))

    def test_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def missing(*argv: str, **kwargs: Any) -> FakeProc:
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(uhubctl_mod.asyncio, "create_subprocess_exec", missing)
        with pytest.raises(PowerControllerError, match="not found"):
            asyncio.run(_ctl().power_off(""))


class TestFromEnv:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in ("DEFIB_UHUBCTL_LOCATION", "DEFIB_UHUBCTL_PORT",
                  "DEFIB_UHUBCTL_COMMAND"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("DEFIB_POWER_TYPE", "uhubctl")

    def test_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_UHUBCTL_LOCATION", "3-2.3")
        monkeypatch.setenv("DEFIB_UHUBCTL_PORT", "3")
        monkeypatch.setenv("DEFIB_UHUBCTL_COMMAND", "sudo uhubctl")
        ctl = power_controller_from_env()
        assert isinstance(ctl, UhubctlController)
        assert ctl._location == "3-2.3"
        assert ctl._port == 3
        assert ctl._command == ["sudo", "uhubctl"]

    def test_missing_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_UHUBCTL_PORT", "3")
        with pytest.raises(PowerControllerError, match="LOCATION"):
            power_controller_from_env()

    def test_missing_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_UHUBCTL_LOCATION", "3-2.3")
        with pytest.raises(PowerControllerError, match="DEFIB_UHUBCTL_PORT"):
            power_controller_from_env()

    @pytest.mark.parametrize("bad", ["x", "0"])
    def test_bad_port(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        monkeypatch.setenv("DEFIB_UHUBCTL_LOCATION", "3-2.3")
        monkeypatch.setenv("DEFIB_UHUBCTL_PORT", bad)
        with pytest.raises(PowerControllerError, match="DEFIB_UHUBCTL_PORT"):
            power_controller_from_env()
