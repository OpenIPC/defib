"""Tests for the recovery session orchestrator."""

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.recovery.events import LogEvent, ProgressEvent
from defib.recovery.session import RecoverySession
from defib.transport.mock import MockTransport

PROFILES_DIR = __import__("pathlib").Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


class TestRecoverySession:
    def test_create_session_standard(self):
        session = RecoverySession(chip="hi3516cv300", firmware_data=b"\x00" * 100)
        assert session.protocol_name == "HiSilicon Standard"

    def test_create_session_v500(self):
        session = RecoverySession(chip="gk7205v500", firmware_data=b"\x00" * 100)
        assert session.protocol_name == "HiSilicon V500"

    def test_create_session_cv6xx(self):
        session = RecoverySession(chip="hi3516cv610", firmware_data=b"\x00" * 100)
        assert session.protocol_name == "HiSilicon CV6xx"

    def test_create_session_unknown_chip(self):
        with pytest.raises(ValueError, match="No protocol found"):
            RecoverySession(chip="unknown_chip_xyz", firmware_data=b"\x00")

    @pytest.mark.asyncio
    async def test_full_standard_session(self):
        """Integration test: full standard protocol session with mock transport."""
        transport = MockTransport(flush_clears_buffer=False)

        # Handshake: 5x 0x20
        transport.enqueue_rx(b"\x20" * 5)
        # ACKs for all frames
        transport.enqueue_rx(ACK_BYTE * 500)

        firmware = bytes(range(256)) * 80  # 20480 bytes

        session = RecoverySession(chip="hi3516cv300", firmware_data=firmware)

        log_events: list[LogEvent] = []
        progress_events: list[ProgressEvent] = []

        result = await session.run(
            transport,
            on_progress=progress_events.append,
            on_log=log_events.append,
        )

        assert result.success
        assert result.elapsed_ms > 0
        assert len(log_events) > 0
        assert len(progress_events) > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chip, spl_len", [
        # 0x5000 blob under an inherited FILELEN of 0x6000: must not be
        # padded out to the profile maximum.
        ("hi3516av300:emmc", 0x5000),
    ])
    async def test_variant_spl_blob_replaces_the_firmware_spl(self, chip, spl_len):
        """`burn -c <chip>:<variant>` must send the board's SPL_BLOB, exactly
        as long as the blob, as the SPL stage."""
        import struct

        from defib.profiles.loader import load_profile

        profile = load_profile(chip, PROFILES_DIR)
        blob = profile.spl_data
        assert blob is not None and len(blob) == spl_len

        transport = MockTransport(flush_clears_buffer=False)
        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 1000)
        firmware = b"\x5a" * 0x8000

        session = RecoverySession(chip=chip, firmware_data=firmware)
        result = await session.run(transport)

        assert result.success
        sent = b"".join(transport.tx_log)
        # SPL DATA frames carry the blob in 1024-byte chunks after a 3-byte
        # frame header; the first chunk is enough to tell whose SPL went out.
        assert blob[:1024] in sent
        head = b"\xfe\x00\xff\x01" + struct.pack(">II", spl_len, profile.spl_address)
        assert head in sent
