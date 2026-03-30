"""Tests for the standard HiSilicon boot protocol."""

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.profiles.loader import load_profile
from defib.recovery.events import Stage
from defib.transport.mock import MockTransport

PROFILES_DIR = __import__("pathlib").Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


class TestHiSiliconStandardMatches:
    def test_matches_known_chip(self):
        assert HiSiliconStandard.matches("hi3516cv300")

    def test_matches_case_insensitive(self):
        assert HiSiliconStandard.matches("HI3516CV300")

    def test_no_match_v500(self):
        assert not HiSiliconStandard.matches("gk7205v500")

    def test_no_match_cv6xx(self):
        assert not HiSiliconStandard.matches("hi3516cv610")


class TestStandardHandshake:
    @pytest.mark.asyncio
    async def test_successful_handshake(self):
        transport = MockTransport()
        # Simulate bootrom sending 5x 0x20
        transport.enqueue_rx(b"\x20\x20\x20\x20\x20")
        # After we send 0xAA, there's nothing more to read during handshake

        protocol = HiSiliconStandard()
        result = await protocol.handshake(transport)

        assert result.success
        assert "Boot mode" in result.message
        # Verify we sent the ACK
        assert ACK_BYTE in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_handshake_with_noise(self):
        transport = MockTransport()
        # Some noise bytes, then 5x 0x20
        transport.enqueue_rx(b"\x00\x00\x15\x20\x20\x20\x20\x20")

        protocol = HiSiliconStandard()
        result = await protocol.handshake(transport)
        assert result.success

    # No timeout test — handshake waits forever for user to power-cycle.
    # User cancels via Ctrl-C, not a timeout.


class TestStandardFirmwareTransfer:
    @pytest.mark.asyncio
    async def test_send_firmware_no_profile(self):
        transport = MockTransport()
        protocol = HiSiliconStandard()
        result = await protocol.send_firmware(transport, b"\x00" * 1024)
        assert not result.success
        assert "No profile" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_ddr_step(self):
        """Test DDR step transfer with ACK responses."""
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516cv300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # Enqueue ACKs: head + data + tail for DDR step
        # Then head + data chunks + tail for SPL
        # Then head + data chunks + tail for U-Boot
        # Each frame send needs one ACK
        ack_count = 200  # Enough ACKs for all frames
        transport.enqueue_rx(ACK_BYTE * ack_count)

        progress_events: list[object] = []
        firmware = bytes(range(256)) * 100  # 25600 bytes

        result = await protocol.send_firmware(
            transport, firmware,
            on_progress=lambda e: progress_events.append(e),
        )

        assert result.success
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed
        assert Stage.COMPLETE in result.stages_completed
        assert len(progress_events) > 0
