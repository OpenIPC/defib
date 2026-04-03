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


class TestFastBootRecovery:
    """Regression tests for fast-booting devices where bootrom window is <100ms.

    Real-world observation (hi3516cv300, hi3516ev300):
    The bootrom sends ~25x 0x20 bytes immediately followed by "System startup"
    text in the same serial burst. The entire bootrom-to-SPI-boot transition
    happens in a single serial read (~0.5s after power-on). If the host doesn't
    respond with 0xAA fast enough, the device boots from flash and the handshake
    "succeeds" (it saw 5x 0x20) but the DDR step fails because the device is
    now in U-Boot shell, not download mode.
    """

    # Captured from real IVGHP203Y-AF (hi3516cv300) at T+0.520s:
    # 25x 0x20 immediately followed by \n\r\n\r\nSystem startup\r\n\nUncompress...
    BOOTROM_BURST_CV300 = (
        b"\x20" * 25
        + b"\x0a\x0d\x0a\x0d\x0a"
        + b"System startup\x0d\x0a\x0a"
        + b"Uncompress........Ok\x0d\x0a"
    )

    # What the device echoes back when receiving a HEAD frame while in U-Boot:
    # 0x65='e', then 0xfe (first byte of HEAD frame) echoed with 0x08 (backspace)
    UBOOT_ECHO = b"\x65" + b"\xfe" * 15 + b"\x08" * 200

    @pytest.mark.asyncio
    async def test_fast_boot_handshake_succeeds_but_ddr_fails(self):
        """Current behavior: handshake detects 0x20 but device already left bootrom.

        The 0xAA response arrives too late — device has moved to SPI boot.
        DDR step then fails because device echoes frames instead of ACKing.
        """
        transport = MockTransport(flush_clears_buffer=True)

        # Bootrom burst: 0x20 * 25 + boot text (all in one read)
        transport.enqueue_rx(self.BOOTROM_BURST_CV300)

        protocol = HiSiliconStandard()
        result = await protocol.handshake(transport)

        # Handshake "succeeds" — it saw 5x 0x20 and sent 0xAA
        assert result.success

        # But the remaining boot text was flushed by flush_input().
        # Now when DDR step tries to talk to the bootrom, the device
        # is actually in U-Boot and echoes back frame data instead of ACKing.
        profile = load_profile("hi3516cv300", PROFILES_DIR)
        protocol.set_profile(profile)

        # Enqueue what the U-Boot shell sends back when it receives frame data
        transport.enqueue_rx(self.UBOOT_ECHO)

        firmware_result = await protocol.send_firmware(transport, b"\x00" * 1024)

        # DDR step fails — the echoed bytes don't match ACK_BYTE
        assert not firmware_result.success
        assert "DDR" in (firmware_result.error or "")

    @pytest.mark.asyncio
    async def test_continuous_ack_sends_aa_before_bootrom(self):
        """With continuous_ack, handshake sends 0xAA before seeing any 0x20.

        This ensures the bootrom sees 0xAA immediately on startup,
        catching the <100ms window that manual response misses.
        """
        transport = MockTransport(flush_clears_buffer=True)

        # Simulate: several timeouts (device hasn't booted yet), then bootrom
        transport.enqueue_rx(b"\x20" * 5)

        protocol = HiSiliconStandard()
        protocol.set_continuous_ack(True)
        result = await protocol.handshake(transport)

        assert result.success
        # Verify 0xAA was sent BEFORE the bootrom pattern was fully consumed
        # (continuous mode sends 0xAA on every loop iteration)
        assert b"\xaa" in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_fast_boot_full_recovery_cv300(self):
        """Full automated recovery must succeed on a fast-booting hi3516cv300.

        This is the target behavior: power-cycle + continuous_ack + recovery
        should work end-to-end even when the bootrom window is very short.
        """
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516cv300", PROFILES_DIR)

        # Simulate successful bootrom entry: 5x 0x20, then ACKs for all frames
        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 200)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)
        protocol.set_continuous_ack(True)

        hs = await protocol.handshake(transport)
        assert hs.success

        firmware = bytes(range(256)) * 100  # 25600 bytes
        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed

    @pytest.mark.asyncio
    async def test_fast_boot_full_recovery_ev300(self):
        """Full automated recovery must succeed on a fast-booting hi3516ev300."""
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516ev300", PROFILES_DIR)

        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 200)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)
        protocol.set_continuous_ack(True)

        hs = await protocol.handshake(transport)
        assert hs.success

        firmware = bytes(range(256)) * 100
        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed


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
