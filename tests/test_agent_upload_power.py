"""Tests for agent upload's power-cycle-into-handshake sequencing."""

from __future__ import annotations

import asyncio

import pytest

from defib.cli.app import _power_cycle_into_handshake
from defib.power.base import PowerController, PowerControllerError
from defib.recovery.events import HandshakeResult
from defib.transport.mock import MockTransport


class RecordingPower(PowerController):
    def __init__(self, events: list[str], fail_on: str | None = None) -> None:
        self.events = events
        self.fail_on = fail_on

    @classmethod
    def name(cls) -> str:
        return "recording"

    async def power_off(self, port: str) -> None:
        self.events.append("off")
        if self.fail_on == "off":
            raise PowerControllerError("relay offline")

    async def power_on(self, port: str) -> None:
        self.events.append("on")
        if self.fail_on == "on":
            raise PowerControllerError("relay offline")

    async def close(self) -> None:
        return None


async def test_handshake_starts_before_power_on() -> None:
    events: list[str] = []

    async def handshake() -> HandshakeResult:
        events.append("handshake")
        await asyncio.sleep(0)
        return HandshakeResult(success=True)

    hs = await _power_cycle_into_handshake(
        RecordingPower(events), MockTransport(), handshake, lambda _m: None,
        off_duration=0.0,
    )
    assert hs.success
    assert events == ["off", "handshake", "on"]


async def test_retries_with_fresh_cycle_on_timeout() -> None:
    events: list[str] = []
    results = iter([None, HandshakeResult(success=True)])

    async def handshake() -> HandshakeResult:
        events.append("handshake")
        result = next(results)
        if result is None:
            await asyncio.sleep(10)  # never answers; times out
        assert result is not None
        return result

    logs: list[str] = []
    hs = await _power_cycle_into_handshake(
        RecordingPower(events), MockTransport(), handshake, logs.append,
        off_duration=0.0, handshake_timeout=0.05,
    )
    assert hs.success
    assert events == ["off", "handshake", "on", "off", "handshake", "on"]
    assert any("attempt 2/2" in m for m in logs)


async def test_gives_up_after_attempts() -> None:
    async def handshake() -> HandshakeResult:
        return HandshakeResult(success=False, message="bad marker")

    hs = await _power_cycle_into_handshake(
        RecordingPower([]), MockTransport(), handshake, lambda _m: None,
        off_duration=0.0, attempts=2,
    )
    assert not hs.success
    assert hs.message == "bad marker"


async def test_power_on_failure_cancels_handshake() -> None:
    cancelled = asyncio.Event()

    async def handshake() -> HandshakeResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return HandshakeResult(success=True)

    with pytest.raises(PowerControllerError):
        await _power_cycle_into_handshake(
            RecordingPower([], fail_on="on"), MockTransport(), handshake,
            lambda _m: None, off_duration=0.0,
        )
    assert cancelled.is_set()
