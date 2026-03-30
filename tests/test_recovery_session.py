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
