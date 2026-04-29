"""Regression tests for the handshake-resilience fixes.

Three bugs were fixed together:

1. The DDR-step error message used to claim "Failed to send DDR step" even
   when the actual failure was the sendFrameForStart handshake or one of the
   PRESTEP/DDRSTEP frame sends.  Now each sub-step returns a distinct
   diagnostic so you can tell handshake-timeout from frame-not-ACKed.

2. Session previously waited a fixed ``sleep(2.0)`` then ``flush_input()``
   after a power cycle.  That misses bytes arriving during/after the flush
   (pyserial's tcflush only drains the kernel buffer at one moment).  We now
   use ``Transport.drain_until_silent`` which loops until the line stays
   quiet for a configurable period — robust because a powered-off chip can't
   transmit.

3. A single transient handshake failure used to fail the entire install.
   Session now retries the power-cycle + handshake + DDR-init phase up to
   ``max_handshake_attempts`` times when programmatic power control is
   available.  Past-DDR failures are not retried (slow, rarely transient).
"""

from __future__ import annotations

import asyncio

import pytest

from defib.power.base import PowerController
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.recovery.events import LogEvent, RecoveryResult, Stage
from defib.recovery.session import RecoverySession
from defib.transport.base import TransportTimeout
from defib.transport.mock import MockTransport


# -----------------------------------------------------------------------------
# Bug 1: distinct DDR-step error attribution
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_ddr_step_returns_handshake_message_on_handshake_timeout(
    monkeypatch,
):
    """If sendFrameForStart times out, the error message names the handshake
    rather than masquerading as a generic DDR failure."""
    from defib.profiles.loader import load_profile

    proto = HiSiliconStandard()
    proto.set_profile(load_profile("hi3516av200"))  # uses prestep + frame-blast

    async def fake_handshake(self, transport, profile):  # noqa: ARG001
        return False

    monkeypatch.setattr(
        HiSiliconStandard, "_send_frame_for_start", fake_handshake
    )

    transport = MockTransport()
    err = await proto._send_ddr_step(transport, proto._profile)
    assert err is not None
    assert "handshake" in err.lower()
    assert "sendFrameForStart" in err


@pytest.mark.asyncio
async def test_send_ddr_step_returns_prestep0_message_on_head_failure(
    monkeypatch,
):
    """If the PRESTEP0 HEAD fails, the error names PRESTEP0 specifically —
    not a generic "Failed to send DDR step"."""
    from defib.profiles.loader import load_profile

    proto = HiSiliconStandard()
    proto.set_profile(load_profile("hi3516av200"))

    async def fake_handshake(self, transport, profile):  # noqa: ARG001
        return True

    async def fake_send_head(self, transport, length, address):  # noqa: ARG001
        return False  # HEAD frame fails to ACK

    monkeypatch.setattr(
        HiSiliconStandard, "_send_frame_for_start", fake_handshake
    )
    monkeypatch.setattr(HiSiliconStandard, "_send_head", fake_send_head)

    transport = MockTransport()
    err = await proto._send_ddr_step(transport, proto._profile)
    assert err is not None
    assert "PRESTEP0" in err
    assert "HEAD" in err


@pytest.mark.asyncio
async def test_send_ddr_step_returns_none_on_success(monkeypatch):
    """Successful DDR step returns None (no error)."""
    from defib.profiles.loader import load_profile

    proto = HiSiliconStandard()
    proto.set_profile(load_profile("hi3516av200"))

    async def ok(*a, **kw):
        return True

    monkeypatch.setattr(HiSiliconStandard, "_send_frame_for_start", ok)
    monkeypatch.setattr(HiSiliconStandard, "_send_head", ok)
    monkeypatch.setattr(HiSiliconStandard, "_send_data", ok)
    monkeypatch.setattr(HiSiliconStandard, "_send_tail", ok)

    transport = MockTransport()
    err = await proto._send_ddr_step(transport, proto._profile)
    assert err is None


@pytest.mark.asyncio
async def test_send_firmware_propagates_ddr_error_message(monkeypatch):
    """send_firmware's RecoveryResult.error includes the specific phase."""
    from defib.profiles.loader import load_profile

    proto = HiSiliconStandard()
    proto.set_profile(load_profile("hi3516av200"))

    async def handshake_fail(self, transport, profile):  # noqa: ARG001
        return False

    monkeypatch.setattr(
        HiSiliconStandard, "_send_frame_for_start", handshake_fail
    )

    transport = MockTransport()
    result = await proto.send_firmware(transport, b"\x00" * 1024)
    assert not result.success
    assert "handshake" in (result.error or "").lower()
    assert "DDR init failed" in (result.error or "")


# -----------------------------------------------------------------------------
# Bug 2: drain_until_silent on Transport
# -----------------------------------------------------------------------------


class _DripTransport(MockTransport):
    """Mock that keeps producing bytes for a configurable duration so we can
    test that drain_until_silent really waits for silence."""

    def __init__(self, drip_until: float, byte_per_call: bytes = b"x") -> None:
        super().__init__(flush_clears_buffer=True)
        self._drip_until = drip_until
        self._byte_per_call = byte_per_call

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        loop = asyncio.get_event_loop()
        if loop.time() < self._drip_until:
            await asyncio.sleep(0.01)
            return self._byte_per_call
        # Past drip deadline — behave like idle line.
        if timeout is not None:
            await asyncio.sleep(min(timeout, 0.001))
        raise TransportTimeout("idle")


@pytest.mark.asyncio
async def test_drain_until_silent_returns_after_quiet_period():
    """drain_until_silent returns once the line stays quiet long enough."""
    loop = asyncio.get_event_loop()
    drip_for = 0.3
    transport = _DripTransport(drip_until=loop.time() + drip_for)

    start = loop.time()
    discarded = await transport.drain_until_silent(
        quiet_period=0.1, max_wait=2.0,
    )
    elapsed = loop.time() - start

    # Should have drained at least some bytes during the drip window.
    assert discarded > 0
    # Should have returned shortly after drip stopped (~drip_for + quiet_period).
    assert elapsed < 1.0, f"took too long: {elapsed:.2f}s"
    assert elapsed >= drip_for, f"returned before drip stopped: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_drain_until_silent_returns_immediately_when_idle():
    """Idle transport returns near-instantly (within the quiet period)."""
    transport = MockTransport()
    loop = asyncio.get_event_loop()

    start = loop.time()
    discarded = await transport.drain_until_silent(
        quiet_period=0.05, max_wait=2.0,
    )
    elapsed = loop.time() - start

    assert discarded == 0
    assert elapsed < 0.5, f"idle drain took too long: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_drain_until_silent_respects_max_wait():
    """If the line never goes quiet, drain_until_silent gives up at max_wait."""
    loop = asyncio.get_event_loop()
    transport = _DripTransport(drip_until=loop.time() + 10.0)

    start = loop.time()
    await transport.drain_until_silent(quiet_period=1.0, max_wait=0.3)
    elapsed = loop.time() - start

    # Should bail out at max_wait, not wait forever.
    assert elapsed < 1.0, f"did not honor max_wait: {elapsed:.2f}s"


# -----------------------------------------------------------------------------
# Bug 3: handshake retry in RecoverySession
# -----------------------------------------------------------------------------


class _FakePowerController(PowerController):
    """Power controller that records calls; useful for retry tests."""

    def __init__(self) -> None:
        self.cycle_count = 0
        self.cycle_ports: list[str] = []

    @classmethod
    def name(cls) -> str:
        return "fake"

    async def power_off(self, port: str) -> None:
        return None

    async def power_on(self, port: str) -> None:
        return None

    async def power_cycle(
        self, port: str, off_duration: float = 3.0,
    ) -> None:  # noqa: ARG002
        self.cycle_count += 1
        self.cycle_ports.append(port)

    async def close(self) -> None:
        return None


class _ScriptedTransport(MockTransport):
    """Transport whose protocol.send_firmware result is scripted."""

    def __init__(self) -> None:
        super().__init__(flush_clears_buffer=False)

    async def drain_until_silent(
        self,
        quiet_period: float = 0.5,  # noqa: ARG002
        max_wait: float = 5.0,  # noqa: ARG002
    ) -> int:
        return 0


@pytest.mark.asyncio
async def test_session_retries_handshake_on_transient_failure(monkeypatch):
    """When DDR-init fails on the first attempt but succeeds on the second,
    the session retries and ultimately succeeds."""
    transport = _ScriptedTransport()
    power = _FakePowerController()

    call_count = {"send_firmware": 0}

    async def scripted_send_firmware(self, transport, firmware, on_progress=None,
                                     spl_override=None, payload_label="U-Boot"):
        # noqa: ARG001
        call_count["send_firmware"] += 1
        if call_count["send_firmware"] == 1:
            return RecoveryResult(
                success=False,
                error="DDR init failed: handshake (sendFrameForStart) timed out",
                stages_completed=[],
            )
        return RecoveryResult(
            success=True,
            stages_completed=[Stage.DDR_INIT, Stage.SPL, Stage.UBOOT, Stage.COMPLETE],
        )

    monkeypatch.setattr(
        HiSiliconStandard, "send_firmware", scripted_send_firmware,
    )

    session = RecoverySession(
        chip="hi3516av200",
        firmware_data=b"\x00" * 1024,
        power_controller=power,
        poe_port="ether8",
    )

    logs: list[LogEvent] = []
    result = await session.run(
        transport,
        on_log=logs.append,
        max_handshake_attempts=2,
    )

    assert result.success, f"expected success, got error: {result.error}"
    assert call_count["send_firmware"] == 2
    assert power.cycle_count == 2  # one cycle per attempt
    # A retry log should have been emitted
    retry_logs = [e for e in logs if "retry" in e.message.lower()]
    assert retry_logs, "expected retry log message"


@pytest.mark.asyncio
async def test_session_does_not_retry_post_ddr_failures(monkeypatch):
    """If DDR succeeded but SPL/U-Boot failed, retrying won't help — and would
    waste 30+ seconds of upload — so we bail out."""
    transport = _ScriptedTransport()
    power = _FakePowerController()

    call_count = {"send_firmware": 0}

    async def post_ddr_failure(self, transport, firmware, on_progress=None,
                               spl_override=None, payload_label="U-Boot"):
        # noqa: ARG001
        call_count["send_firmware"] += 1
        return RecoveryResult(
            success=False,
            error="Failed to send SPL",
            stages_completed=[Stage.DDR_INIT],
        )

    monkeypatch.setattr(
        HiSiliconStandard, "send_firmware", post_ddr_failure,
    )

    session = RecoverySession(
        chip="hi3516av200",
        firmware_data=b"\x00" * 1024,
        power_controller=power,
        poe_port="ether8",
    )

    result = await session.run(transport, max_handshake_attempts=3)

    assert not result.success
    # Only ONE attempt — no retry past DDR
    assert call_count["send_firmware"] == 1
    assert power.cycle_count == 1


@pytest.mark.asyncio
async def test_session_no_retry_without_power_control(monkeypatch):
    """Without programmatic power control, retries are pointless because the
    user would need to physically re-cycle.  Should attempt only once."""
    transport = _ScriptedTransport()

    call_count = {"send_firmware": 0}

    async def always_fail(self, transport, firmware, on_progress=None,
                         spl_override=None, payload_label="U-Boot"):
        # noqa: ARG001
        call_count["send_firmware"] += 1
        return RecoveryResult(
            success=False,
            error="DDR init failed: handshake (sendFrameForStart) timed out",
            stages_completed=[],
        )

    async def fake_handshake(self, transport, on_progress=None):  # noqa: ARG002
        from defib.recovery.events import HandshakeResult
        return HandshakeResult(success=True, message="ok")

    monkeypatch.setattr(HiSiliconStandard, "send_firmware", always_fail)
    monkeypatch.setattr(HiSiliconStandard, "handshake", fake_handshake)

    session = RecoverySession(
        chip="hi3516cv300",  # non-frame-blast chip so handshake is separate
        firmware_data=b"\x00" * 1024,
    )

    result = await session.run(transport, max_handshake_attempts=5)

    assert not result.success
    assert call_count["send_firmware"] == 1


@pytest.mark.asyncio
async def test_session_max_attempts_respected(monkeypatch):
    """All attempts fail → final result reports failure with last error,
    and we don't keep retrying forever."""
    transport = _ScriptedTransport()
    power = _FakePowerController()

    call_count = {"send_firmware": 0}

    async def always_fail(self, transport, firmware, on_progress=None,
                         spl_override=None, payload_label="U-Boot"):
        # noqa: ARG001
        call_count["send_firmware"] += 1
        return RecoveryResult(
            success=False,
            error="DDR init failed: PRESTEP0 HEAD frame not ACKed",
            stages_completed=[],
        )

    monkeypatch.setattr(HiSiliconStandard, "send_firmware", always_fail)

    session = RecoverySession(
        chip="hi3516av200",
        firmware_data=b"\x00" * 1024,
        power_controller=power,
        poe_port="ether8",
    )

    result = await session.run(transport, max_handshake_attempts=3)

    assert not result.success
    assert "PRESTEP0" in (result.error or "")
    assert call_count["send_firmware"] == 3
    assert power.cycle_count == 3
