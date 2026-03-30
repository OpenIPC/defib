"""Tests for the recording and replay transports."""

import pytest

from defib.capture.format import Direction
from defib.capture.recorder import RecordingTransport
from defib.capture.replayer import ReplayTransport
from defib.capture.format import CaptureFile
from defib.protocol.crc import ACK_BYTE
from defib.transport.mock import MockTransport


class TestRecordingTransport:
    @pytest.mark.asyncio
    async def test_records_writes(self):
        inner = MockTransport()
        recorder = RecordingTransport(inner, chip="test")

        await recorder.write(b"\xfe\x00\xff\x01")
        await recorder.write(b"\xda\x01")

        assert len(recorder.capture.records) == 2
        assert recorder.capture.records[0].direction == Direction.TX
        assert recorder.capture.records[0].data == b"\xfe\x00\xff\x01"
        assert recorder.capture.records[1].data == b"\xda\x01"

    @pytest.mark.asyncio
    async def test_records_reads(self):
        inner = MockTransport()
        inner.enqueue_rx(b"\xaa\xaa\xaa")
        recorder = RecordingTransport(inner, chip="test")

        data = await recorder.read(2, timeout=1.0)

        assert data == b"\xaa\xaa"
        rx_records = [r for r in recorder.capture.records if r.direction == Direction.RX]
        assert len(rx_records) == 1
        assert rx_records[0].data == b"\xaa\xaa"

    @pytest.mark.asyncio
    async def test_passthrough(self):
        """Data passes through to inner transport."""
        inner = MockTransport()
        inner.enqueue_rx(b"\x20\x20\x20")
        recorder = RecordingTransport(inner)

        await recorder.write(b"\xaa")
        data = await recorder.read(3, timeout=1.0)

        assert data == b"\x20\x20\x20"
        assert inner.tx_log == [b"\xaa"]

    @pytest.mark.asyncio
    async def test_timestamps_increase(self):
        inner = MockTransport()
        inner.enqueue_rx(b"\xaa")
        recorder = RecordingTransport(inner)

        await recorder.write(b"\x01")
        await recorder.read(1, timeout=1.0)

        assert len(recorder.capture.records) == 2
        assert recorder.capture.records[0].timestamp_us <= recorder.capture.records[1].timestamp_us

    @pytest.mark.asyncio
    async def test_chip_metadata(self):
        inner = MockTransport()
        recorder = RecordingTransport(inner, chip="hi3516cv300", baudrate=9600)

        assert recorder.capture.chip == "hi3516cv300"
        assert recorder.capture.baudrate == 9600


class TestReplayTransport:
    @pytest.mark.asyncio
    async def test_replay_rx_data(self):
        cap = CaptureFile(chip="test")
        cap.add_rx(0, b"\x20\x20\x20\x20\x20")
        cap.add_rx(100, b"\xaa")

        replay = ReplayTransport(cap)
        data1 = await replay.read(5, timeout=1.0)
        data2 = await replay.read(1, timeout=1.0)

        assert data1 == b"\x20\x20\x20\x20\x20"
        assert data2 == b"\xaa"

    @pytest.mark.asyncio
    async def test_replay_captures_tx(self):
        cap = CaptureFile()
        cap.add_rx(0, b"\xaa")

        replay = ReplayTransport(cap)
        await replay.write(b"\xfe\x00\xff\x01")

        assert replay.all_tx_data == b"\xfe\x00\xff\x01"

    @pytest.mark.asyncio
    async def test_replay_complete(self):
        cap = CaptureFile()
        cap.add_rx(0, b"\xaa")

        replay = ReplayTransport(cap)
        assert not replay.replay_complete

        await replay.read(1, timeout=1.0)
        assert replay.replay_complete

    @pytest.mark.asyncio
    async def test_replay_timeout_when_empty(self):
        cap = CaptureFile()  # No RX data

        replay = ReplayTransport(cap)
        from defib.transport.base import TransportTimeout
        with pytest.raises(TransportTimeout):
            await replay.read(1, timeout=0.01)

    @pytest.mark.asyncio
    async def test_replay_bytes_waiting(self):
        cap = CaptureFile()
        cap.add_rx(0, b"\x01\x02\x03")

        replay = ReplayTransport(cap)
        assert await replay.bytes_waiting() == 3
        await replay.read(1, timeout=1.0)
        assert await replay.bytes_waiting() == 2

    @pytest.mark.asyncio
    async def test_replay_unread(self):
        cap = CaptureFile()
        cap.add_rx(0, b"\x01\x02\x03")

        replay = ReplayTransport(cap)
        data = await replay.read(3, timeout=1.0)
        assert data == b"\x01\x02\x03"

        await replay.unread(b"\x03")
        data2 = await replay.read(1, timeout=1.0)
        assert data2 == b"\x03"

    @pytest.mark.asyncio
    async def test_roundtrip_record_replay(self):
        """Record a session with MockTransport, then replay it."""
        # Record phase
        inner = MockTransport(flush_clears_buffer=False)
        inner.enqueue_rx(b"\x20" * 5 + ACK_BYTE * 10)

        from defib.capture.recorder import RecordingTransport
        recorder = RecordingTransport(inner, chip="roundtrip_test")

        await recorder.write(b"\xaa")  # Handshake ACK
        for _ in range(5):
            await recorder.read(1, timeout=1.0)
        await recorder.write(b"\xfe\x00\xff\x01")  # HEAD frame

        # Replay phase: replays the RX data that was recorded (5x 0x20)
        replay = ReplayTransport(recorder.capture)
        rx1 = await replay.read(5, timeout=1.0)
        assert rx1 == b"\x20" * 5  # The 5 bootrom marker bytes we read
