"""Tests for protocol robustness: rehandshake after SPL and non-fatal U-Boot TAIL.

These are regression tests for fixes discovered on hi3516av200 where:
1. The SPL re-sends 0x20 bootmode markers after DDR init, requiring a second
   0xAA handshake before U-Boot HEAD frames are accepted.
2. The U-Boot TAIL frame may not be ACKed (device considers transfer complete
   based on byte count from HEAD), so TAIL failure is non-fatal.
"""

import pathlib
import time

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.hisilicon_standard import (
    BOOTMODE_ACK,
    BOOTMODE_MARKER,
    HiSiliconStandard,
)
from defib.profiles.loader import load_profile
from defib.recovery.events import Stage
from defib.transport.mock import MockTransport

PROFILES_DIR = pathlib.Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


class TestRehandshake:
    """Tests for _rehandshake() — the post-SPL bootmode re-entry."""

    @pytest.mark.asyncio
    async def test_rehandshake_with_markers(self):
        """When SPL sends 0x20 markers, rehandshake sends 0xAA."""
        transport = MockTransport(flush_clears_buffer=False)
        transport.enqueue_rx(BOOTMODE_MARKER * 10)  # 10x 0x20

        result = await HiSiliconStandard._rehandshake(transport)

        assert result is True
        assert BOOTMODE_ACK in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_rehandshake_quiet_line(self):
        """When line is quiet (no markers), rehandshake returns True immediately."""
        transport = MockTransport(flush_clears_buffer=False)
        # No data enqueued — read will timeout

        t0 = time.monotonic()
        result = await HiSiliconStandard._rehandshake(transport)
        elapsed = time.monotonic() - t0

        assert result is True
        # Should return quickly (within the 0.2s timeout), not wait 5s
        assert elapsed < 1.0
        # No ACK sent — device was already ready
        assert BOOTMODE_ACK not in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_rehandshake_ignores_newlines(self):
        """Newlines (0x0A, 0x0D) mixed into marker stream are ignored."""
        transport = MockTransport(flush_clears_buffer=False)
        transport.enqueue_rx(b"\x20\x20\x0a\x20\x0d\x20\x20")

        result = await HiSiliconStandard._rehandshake(transport)

        assert result is True
        assert BOOTMODE_ACK in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_rehandshake_partial_markers(self):
        """If fewer than 5 markers arrive then silence, ACK is still sent."""
        transport = MockTransport(flush_clears_buffer=False)
        transport.enqueue_rx(BOOTMODE_MARKER * 3)  # only 3 markers

        result = await HiSiliconStandard._rehandshake(transport)

        assert result is True
        assert BOOTMODE_ACK in transport.all_tx_data

    @pytest.mark.asyncio
    async def test_rehandshake_unexpected_byte(self):
        """Non-marker, non-newline byte means device is ready (no re-handshake)."""
        transport = MockTransport(flush_clears_buffer=False)
        transport.enqueue_rx(b"\x42")  # some random byte

        result = await HiSiliconStandard._rehandshake(transport)

        assert result is True
        assert BOOTMODE_ACK not in transport.all_tx_data


class TestNonFatalUbootTail:
    """Verify U-Boot transfer succeeds even when TAIL is not ACKed."""

    @pytest.mark.asyncio
    async def test_uboot_succeeds_without_tail_ack(self):
        """U-Boot transfer reports success even when TAIL frame is not ACKed.

        Regression test: hi3516av200 SPL responds with 0xFE to TAIL instead
        of 0xAA.  All data frames ARE ACKed, so the transfer is complete.
        """
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516cv300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # ACKs for: DDR(head+data+tail=3) + SPL(head+chunks+tail) + U-Boot(head+chunks)
        # Enough for everything EXCEPT the U-Boot TAIL
        firmware = bytes(range(256)) * 100  # 25600 bytes
        # Over-provision ACKs — the U-Boot tail will consume a non-ACK byte
        transport.enqueue_rx(ACK_BYTE * 200 + b"\xfe")

        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed
        assert Stage.COMPLETE in result.stages_completed

    @pytest.mark.asyncio
    async def test_uboot_succeeds_with_tail_ack(self):
        """Normal path: TAIL is ACKed (e.g. hi3516ev300). Must still work."""
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516ev300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        transport.enqueue_rx(ACK_BYTE * 200)
        firmware = bytes(range(256)) * 100

        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.UBOOT in result.stages_completed


class TestRehandshakeIntegration:
    """End-to-end tests: full firmware transfer with rehandshake."""

    @pytest.mark.asyncio
    async def test_full_transfer_with_rehandshake(self):
        """Simulate hi3516av200-style transfer: SPL re-sends markers before U-Boot.

        Flow: handshake → DDR → SPL → markers → rehandshake → U-Boot
        """
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516cv300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # Phase 1: DDR step ACKs (head + data + tail = 3 ACKs)
        # Phase 2: SPL ACKs (head + spl_chunks + tail)
        spl_size = profile.spl_max_size
        spl_chunks = (spl_size + 1023) // 1024
        spl_acks = 1 + spl_chunks + 1  # head + data + tail

        # Phase 3: Rehandshake markers (0x20 * 5)
        # Phase 4: U-Boot ACKs (head + uboot_chunks + tail-or-not)
        firmware = bytes(range(256)) * 100  # 25600 bytes
        uboot_chunks = (len(firmware) + 1023) // 1024
        uboot_acks = 1 + uboot_chunks + 1  # head + data + tail

        transport.enqueue_rx(
            ACK_BYTE * (3 + spl_acks)       # DDR + SPL
            + BOOTMODE_MARKER * 5           # rehandshake markers
            + ACK_BYTE * uboot_acks         # U-Boot
        )

        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.DDR_INIT in result.stages_completed
        assert Stage.SPL in result.stages_completed
        assert Stage.UBOOT in result.stages_completed
        assert Stage.COMPLETE in result.stages_completed

        # Verify rehandshake ACK was sent (0xAA appears after SPL phase)
        tx = transport.all_tx_data
        assert tx.count(BOOTMODE_ACK) >= 2  # initial handshake would be separate

    @pytest.mark.asyncio
    async def test_full_transfer_no_rehandshake_needed(self):
        """Simulate hi3516ev300-style transfer: no markers between SPL and U-Boot.

        The rehandshake should return quickly (timeout) and not break the flow.
        """
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516ev300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # Just ACKs — no markers between SPL and U-Boot
        transport.enqueue_rx(ACK_BYTE * 200)
        firmware = bytes(range(256)) * 100

        t0 = time.monotonic()
        result = await protocol.send_firmware(transport, firmware)
        elapsed = time.monotonic() - t0

        assert result.success
        assert Stage.UBOOT in result.stages_completed
        # Should complete in reasonable time (rehandshake timeout + transfer)
        # The rehandshake adds at most 0.2s timeout
        assert elapsed < 5.0

    @pytest.mark.asyncio
    async def test_spl_tail_still_required(self):
        """SPL TAIL failure must still be fatal (only U-Boot TAIL is non-fatal)."""
        transport = MockTransport(flush_clears_buffer=False)
        profile = load_profile("hi3516cv300", PROFILES_DIR)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        # Enough ACKs for DDR (3) + SPL head + SPL data, but NOT SPL tail
        spl_size = profile.spl_max_size
        spl_chunks = (spl_size + 1023) // 1024
        # DDR: head(1) + data(1) + tail(1) = 3
        # SPL: head(1) + data(spl_chunks) = 1 + spl_chunks
        # Then NO MORE ACKs — SPL tail will fail
        transport.enqueue_rx(ACK_BYTE * (3 + 1 + spl_chunks))

        firmware = bytes(range(256)) * 100
        result = await protocol.send_firmware(transport, firmware)

        assert not result.success
        assert "SPL" in (result.error or "")
