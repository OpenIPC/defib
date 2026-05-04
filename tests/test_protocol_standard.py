"""Tests for the standard HiSilicon boot protocol."""

import asyncio

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.profiles.loader import load_profile
from defib.recovery.events import Stage
from defib.transport.base import TransportTimeout
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
    async def test_power_off_detection_triggers_flood(self):
        """Manual mode: device sends data, goes silent, handshake floods 0xAA.

        Regression test for TUI hang: continuous 0xAA to a running device
        generates 0x07 (BEL) echoes that break 0x20 detection. Fix: only
        start flooding after detecting power-off (timeout after data).
        """
        transport = MockTransport(flush_clears_buffer=True)

        # Phase 1: Running device sends non-0x20 data (e.g. shell output)
        transport.enqueue_rx(b"\x07\x07\x07")

        protocol = HiSiliconStandard()
        # Default: continuous_ack=False (manual/TUI mode)
        assert not protocol._continuous_ack

        # Phase 2+3 will be fed after the running-device data is consumed
        # and a timeout triggers the power-off detection.
        # We use a background task to simulate the reboot delay.
        async def simulate_reboot():
            # Wait for the handshake to consume the 0x07 bytes and hit timeouts
            await asyncio.sleep(0.15)
            # Device reboots: bootrom sends 0x20 pattern + ACKs for frames
            transport.enqueue_rx(b"\x20" * 5)

        reboot_task = asyncio.create_task(simulate_reboot())

        result = await protocol.handshake(transport)
        await reboot_task

        assert result.success
        # Verify 0xAA was sent (flooding started after power-off detection)
        assert b"\xaa" in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_no_flood_while_device_running(self):
        """Manual mode: no 0xAA sent while device is active (avoids 0x07 echo).

        If we flood 0xAA to a running Linux/U-Boot, it echoes 0x07 (BEL)
        which pollutes the buffer and prevents bootrom detection.
        """
        transport = MockTransport(flush_clears_buffer=True)

        # Running device sends shell output, then bootrom pattern
        # (no timeout gap = device never went silent = no flooding)
        transport.enqueue_rx(b"shell output\r\n" + b"\x20" * 5)

        protocol = HiSiliconStandard()
        result = await protocol.handshake(transport)

        assert result.success
        # 0xAA should only appear once (the final ACK after 5x 0x20),
        # NOT during the "shell output" phase
        tx = transport.all_tx_data
        assert tx == b"\xaa"

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


class TestDetectSplSize:
    """Regression tests for _detect_spl_size — see PR #55 + gzip-detection fix.

    Bug history:
      - PR #55 added LZMA-header detection so SVB-enabled av200 SPL (>profile_max)
        wasn't truncated. Used max(detected, profile_max).
      - hi3516av300 OpenIPC builds use gzip (not LZMA) for the embedded U-Boot
        and the SPL is more compact than HiTool's reference. With max(), defib
        sent profile_max (24KB) which overshoots the 21KB code into bootrom RAM
        and hangs the SPL transfer mid-chunk.
      - Fix: also detect gzip; trust the detected boundary regardless of
        profile_max; round DOWN to 1KB so we never spill into compressed bytes.
    """

    @staticmethod
    def _detect(firmware: bytes, profile_max: int = 0x6000) -> int:
        return HiSiliconStandard._detect_spl_size(firmware, profile_max)

    def test_gzip_header_detected_below_profile_max(self):
        # Real av300 layout: gzip at 0x52F0 inside a profile_max=0x6000 window.
        firmware = bytes(0x52F0) + b"\x1f\x8b\x08" + b"\x00" * 100
        # Round DOWN to 1 KB — never include any byte of the gzip payload.
        assert self._detect(firmware, profile_max=0x6000) == 0x5000

    def test_gzip_header_above_profile_max(self):
        firmware = bytes(0x6800) + b"\x1f\x8b\x08" + b"\x00" * 100
        assert self._detect(firmware, profile_max=0x6000) == 0x6800

    def test_lzma_header_detected_below_profile_max(self):
        # 0x5D + dict_size 0x10000 (64K) is a valid LZMA header.
        firmware = bytes(0x5400) + b"\x5d\x00\x00\x01\x00" + b"\x00" * 100
        assert self._detect(firmware, profile_max=0x6000) == 0x5400

    def test_lzma_header_rounds_down(self):
        # Header at 0x53F0 rounds DOWN to 0x5000 (not up to 0x5400).
        firmware = bytes(0x53F0) + b"\x5d\x00\x00\x01\x00" + b"\x00" * 100
        assert self._detect(firmware, profile_max=0x6000) == 0x5000

    def test_lzma_invalid_dict_size_ignored(self):
        # 0x5D followed by garbage dict size is not a real LZMA header.
        firmware = bytes(0x5400) + b"\x5d\xff\xff\xff\xff" + b"\x00" * 100
        # Falls through to profile_max because no valid marker found.
        assert self._detect(firmware, profile_max=0x6000) == 0x6000

    def test_no_marker_falls_back_to_profile_max(self):
        firmware = b"\xa5" * 0x10000
        assert self._detect(firmware, profile_max=0x6000) == 0x6000

    def test_marker_before_search_window_ignored(self):
        # The scan starts at 0x4000 — markers in the .reg region don't count.
        firmware = bytes(0x100) + b"\x1f\x8b\x08" + bytes(0x6000)
        assert self._detect(firmware, profile_max=0x6000) == 0x6000


class TestZeroLongFfRuns:
    """Regression tests for _zero_long_ff_runs.

    The hi3516cv500-family bootrom hangs mid-DATA frame when payload contains
    >=12 consecutive 0xFF bytes (likely a UART RX-path quirk in the bootrom).
    These runs only appear as inert padding between SPL code and the
    compressed U-Boot payload, so zeroing them is safe.
    """

    @staticmethod
    def _zero(firmware: bytes, threshold: int = 12) -> bytes:
        return HiSiliconStandard._zero_long_ff_runs(firmware, threshold)

    def test_run_at_threshold_is_zeroed(self):
        firmware = b"\xaa" + b"\xff" * 12 + b"\xbb"
        out = self._zero(firmware)
        assert out == b"\xaa" + b"\x00" * 12 + b"\xbb"

    def test_run_below_threshold_preserved(self):
        firmware = b"\xaa" + b"\xff" * 11 + b"\xbb"
        assert self._zero(firmware) == firmware

    def test_run_at_end_of_buffer_zeroed(self):
        firmware = b"\xaa" + b"\xff" * 16
        out = self._zero(firmware)
        assert out == b"\xaa" + b"\x00" * 16

    def test_no_runs_returns_unchanged(self):
        firmware = bytes(range(256)) * 4
        assert self._zero(firmware) is firmware  # short-circuits, no copy

    def test_multiple_runs_all_zeroed(self):
        firmware = b"\xff" * 12 + b"\xaa" + b"\xff" * 13 + b"\xbb"
        out = self._zero(firmware)
        assert out == b"\x00" * 12 + b"\xaa" + b"\x00" * 13 + b"\xbb"

    def test_av300_padding_pattern(self):
        # The exact pattern from hi3516av300 u-boot.bin at offset 0x52E0:
        # 4 SPL bytes, 12 0xFF padding, gzip header.
        firmware = bytes([0x04, 0x00, 0x02, 0x12]) + b"\xff" * 12 + b"\x1f\x8b\x08"
        out = self._zero(firmware)
        assert out == bytes([0x04, 0x00, 0x02, 0x12]) + b"\x00" * 12 + b"\x1f\x8b\x08"


class TestSplTailNonFatalForFrameBlast:
    """Regression test for av200/av300 SPL TAIL handling.

    On chips with PRESTEP0 (frame-blast handshake), the SPL detaches the
    bootrom protocol handler as soon as it has received all declared bytes,
    so the TAIL frame is never ACKed. Treating that as fatal stalls the
    install at SPL completion. (Mirrors the existing U-Boot TAIL behavior.)
    """

    @pytest.mark.asyncio
    async def test_spl_tail_no_ack_succeeds_for_av300(self):
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516av300", PROFILES_DIR)
        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # av300 profile_max is 0x6000 = 24576 bytes = 24 chunks of 1024.
        # No marker in zeros, so _detect_spl_size falls back to profile_max.
        firmware = b"\x00" * profile.spl_max_size
        spl_chunks = (profile.spl_max_size + 1023) // 1024

        # ACKs for SPL HEAD + chunks. Deliberately NO ACK for TAIL —
        # TAIL retries time out, then fall through (because prestep_data is set).
        transport.enqueue_rx(ACK_BYTE * (1 + spl_chunks))

        ok = await protocol._send_spl(transport, firmware, profile)
        assert ok, "av300 SPL must succeed even without TAIL ACK"

    @pytest.mark.asyncio
    async def test_spl_tail_no_ack_fails_for_chip_without_prestep(self):
        # On chips WITHOUT prestep_data, TAIL ACK is required — verify the
        # legacy strict behavior is preserved.
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("gk7201v300", PROFILES_DIR)  # no PRESTEP0
        assert profile.prestep_data is None
        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        firmware = b"\x00" * profile.spl_max_size
        spl_chunks = (profile.spl_max_size + 1023) // 1024
        # ACKs for HEAD + chunks, no TAIL ACK.
        transport.enqueue_rx(ACK_BYTE * (1 + spl_chunks))

        ok = await protocol._send_spl(transport, firmware, profile)
        assert not ok, "chips without prestep_data must still treat SPL TAIL as fatal"


class TestWriteTimeoutRetry:
    """Regression test for write-timeout handling in _send_frame_with_retry.

    Previously, transport.write() blocked outside the try/except, so a hung
    write (e.g. PL2303 TX buffer not draining) would propagate a raw
    TransportTimeout up to the caller — bypassing the retry loop entirely.
    Now the write is inside the try block: a transient write failure is
    treated like an ACK timeout and retried.
    """

    @pytest.mark.asyncio
    async def test_transient_write_timeout_is_retried(self):
        class FlakeyTransport(MockTransport):
            """Times out the first N writes, then behaves normally."""
            def __init__(self, fail_writes: int) -> None:
                super().__init__(flush_clears_buffer=False)
                self._remaining_failures = fail_writes

            async def write(self, data: bytes) -> None:
                if self._remaining_failures > 0:
                    self._remaining_failures -= 1
                    raise TransportTimeout("simulated write hang")
                await super().write(data)

        transport = FlakeyTransport(fail_writes=2)
        transport.enqueue_rx(ACK_BYTE)

        protocol = HiSiliconStandard()
        # _send_head goes through _send_frame_with_retry.
        ok = await protocol._send_head(transport, length=64, address=0x04017000)
        assert ok, "retry loop must recover from transient write timeouts"
