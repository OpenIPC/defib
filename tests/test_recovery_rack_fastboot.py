"""Tests for the pod-side fastboot host adapter."""

from __future__ import annotations

from typing import Any

import pytest

from defib.power.rack import RackController
from defib.profiles.loader import load_profile
from defib.recovery.events import RecoveryResult, Stage
from defib.recovery.rack_fastboot import run_rack_fastboot


class FakeRack(RackController):
    """RackController that captures the fastboot() call and returns a
    canned response instead of doing HTTP."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(host="example", port=8080)
        self._response = response
        self.last_call: dict[str, Any] = {}

    async def fastboot(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        self.last_call = kwargs
        return self._response


def _hi3516ev300_uboot_stub() -> bytes:
    """A minimal byte blob that has a gzip marker where the host expects
    the SPL boundary, so `_detect_spl_size` resolves to a real offset
    and not the profile fallback."""
    # gzip header `1f 8b 08` somewhere past 0x4000 makes _detect_spl_size
    # return that offset (rounded down to 0x400).
    blob = bytearray(0x10000)
    # Plausible-looking SPL prefix
    blob[:0x100] = bytes(range(256))
    # gzip marker at 0x4400 → SPL size = 0x4400 (= 17408 bytes)
    blob[0x4400:0x4403] = b"\x1f\x8b\x08"
    return bytes(blob)


class TestRunRackFastboot:
    @pytest.mark.asyncio
    async def test_success_returns_stages_through_complete(self) -> None:
        rack = FakeRack({
            "success": True, "last_phase": "done",
            "elapsed_ms": 4500, "handshake_markers": 5,
        })
        result = await run_rack_fastboot(rack, "hi3516ev300", _hi3516ev300_uboot_stub())

        assert isinstance(result, RecoveryResult)
        assert result.success is True
        assert result.error is None
        assert Stage.COMPLETE in result.stages_completed
        assert Stage.HANDSHAKE in result.stages_completed
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed

    @pytest.mark.asyncio
    async def test_failure_at_prestep0_reports_partial_stages(self) -> None:
        rack = FakeRack({
            "success": False, "last_phase": "prestep0",
            "failed_phase": "prestep0", "error": "PRESTEP0 HEAD",
            "elapsed_ms": 200, "handshake_markers": 5,
        })
        result = await run_rack_fastboot(rack, "hi3516ev300", _hi3516ev300_uboot_stub())

        assert result.success is False
        assert "PRESTEP0 HEAD" in result.error
        assert "prestep0" in result.error
        # Got past handshake but not past SPL
        assert Stage.HANDSHAKE in result.stages_completed
        assert Stage.SPL not in result.stages_completed
        assert Stage.COMPLETE not in result.stages_completed

    @pytest.mark.asyncio
    async def test_blob_includes_correct_profile_addresses(self) -> None:
        rack = FakeRack({"success": True, "last_phase": "done", "elapsed_ms": 0})
        profile = load_profile("hi3516ev300")
        await run_rack_fastboot(rack, "hi3516ev300", _hi3516ev300_uboot_stub())

        assert rack.last_call["spl_address"]      == profile.spl_address
        assert rack.last_call["ddr_step_address"] == profile.ddr_step_address
        assert rack.last_call["uboot_address"]    == profile.uboot_address
        assert rack.last_call["prestep0"]         == (profile.prestep_data or b"")
        assert rack.last_call["ddrstep0"]         == profile.ddr_step_data
        assert rack.last_call["prestep1"]         == profile.prestep1_data

    @pytest.mark.asyncio
    async def test_agent_payload_override_routes_separately(self) -> None:
        """When agent_payload is given, SPL boundary scans the AGENT bytes
        (matching the agent-flash flow) and the SPL truncation comes from
        the firmware blob — same behaviour as HiSiliconStandard._send_spl."""
        rack = FakeRack({"success": True, "last_phase": "done", "elapsed_ms": 0})
        firmware = _hi3516ev300_uboot_stub()
        agent_bytes = b"AGENT" * 1000

        await run_rack_fastboot(rack, "hi3516ev300", firmware, agent_payload=agent_bytes)

        # spl bytes derived from firmware (with FF-run zeroing), not agent
        assert rack.last_call["spl"].startswith(firmware[:0x100])
        # agent payload passed through verbatim
        assert rack.last_call["agent"] == agent_bytes
