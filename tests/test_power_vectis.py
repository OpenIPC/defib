"""Tests for the OpenIPC Vectis UART-bridge power controller."""

from __future__ import annotations

import asyncio

import pytest

from defib.power.base import PowerControllerError
from defib.power.vectis import VectisController
from defib.transport.mock import MockTransport


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeRfc2217Transport(MockTransport):
    """MockTransport plus the RFC 2217 modem-control surface that
    VectisController exercises.  Tracks the order of operations so
    tests can assert the off → sleep → on sequence."""

    def __init__(self) -> None:
        super().__init__()
        self.actions: list[tuple[str, bool]] = []

    async def set_dtr(self, active: bool) -> None:
        self.actions.append(("dtr", active))

    async def set_rts(self, active: bool) -> None:
        self.actions.append(("rts", active))

    async def set_baudrate(self, baud: int) -> None:
        self.actions.append(("baud", baud))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------

class TestVectisFromEnv:
    def test_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFIB_VECTIS_HOST", raising=False)
        with pytest.raises(PowerControllerError, match="DEFIB_VECTIS_HOST"):
            VectisController.from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_VECTIS_HOST", "172.17.32.17")
        monkeypatch.delenv("DEFIB_VECTIS_PORT", raising=False)
        ctrl = VectisController.from_env()
        assert ctrl._host == "172.17.32.17"
        assert ctrl._port == 35240  # upstream default

    def test_custom_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_VECTIS_HOST", "10.0.0.1")
        monkeypatch.setenv("DEFIB_VECTIS_PORT", "35241")
        ctrl = VectisController.from_env()
        assert ctrl._host == "10.0.0.1"
        assert ctrl._port == 35241


# ---------------------------------------------------------------------------
# Off / On are not separately addressable
# ---------------------------------------------------------------------------

class TestOffOnRaise:
    async def test_power_off_raises(self) -> None:
        ctrl = VectisController(host="x", port=1)
        with pytest.raises(PowerControllerError, match="off/on are not"):
            await ctrl.power_off("anything")

    async def test_power_on_raises(self) -> None:
        ctrl = VectisController(host="x", port=1)
        with pytest.raises(PowerControllerError, match="off/on are not"):
            await ctrl.power_on("anything")


# ---------------------------------------------------------------------------
# Shared-transport mode (the path the CLI takes for live recovery)
# ---------------------------------------------------------------------------

class TestSharedTransport:
    async def test_power_cycle_toggles_dtr_rts(self) -> None:
        """Off DTR, off RTS, sleep, on RTS, on DTR — exactly four
        SET-CONTROL operations through the transport, no in-band
        bytes written."""
        transport = FakeRfc2217Transport()
        ctrl = VectisController(host="x", port=1, pulse_seconds=0.0)
        ctrl.attach_transport(transport)

        await ctrl.power_cycle("ignored")

        assert transport.actions == [
            ("dtr", False),
            ("rts", False),
            ("rts", True),
            ("dtr", True),
        ]
        # Crucially: nothing was written in-band.  Old controller wrote
        # b"\x10" through the data path, which would have been escaped
        # or filtered by RFC 2217.
        assert transport.tx_log == []

    async def test_close_does_not_close_shared_transport(self) -> None:
        transport = FakeRfc2217Transport()
        ctrl = VectisController(host="x", port=1, pulse_seconds=0.0)
        ctrl.attach_transport(transport)

        await ctrl.close()

        assert transport._closed is False

    async def test_pulse_actually_waits(self) -> None:
        transport = FakeRfc2217Transport()
        ctrl = VectisController(host="x", port=1, pulse_seconds=0.05)
        ctrl.attach_transport(transport)

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await ctrl.power_cycle("")
        elapsed = loop.time() - t0

        assert elapsed >= 0.04
        assert elapsed < 1.0

    async def test_legacy_fallback_when_transport_lacks_modem_control(self) -> None:
        """If the transport doesn't expose set_dtr/set_rts (e.g. raw
        SocketTransport against a pre-RFC-2217 Vectis), fall back to
        writing a single Ctrl+P byte."""
        transport = MockTransport()  # no set_dtr / set_rts
        ctrl = VectisController(host="x", port=1, pulse_seconds=0.0)
        ctrl.attach_transport(transport)

        await ctrl.power_cycle("ignored")

        assert transport.tx_log == [b"\x10"]


# ---------------------------------------------------------------------------
# Standalone mode (the controller opens its own RFC 2217 connection)
# ---------------------------------------------------------------------------

class TestStandalone:
    async def test_close_is_idempotent(self) -> None:
        ctrl = VectisController(host="x", port=1, pulse_seconds=0.0)
        # No transport attached, no connection ever opened — close
        # should be a safe no-op.
        await ctrl.close()
        await ctrl.close()
