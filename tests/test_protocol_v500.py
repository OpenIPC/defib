"""Tests for the V500 boot protocol."""

import struct

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.hisilicon_v500 import HiSiliconV500, V500_SOCS
from defib.recovery.events import Stage
from defib.transport.mock import MockTransport


class TestV500Matches:
    def test_matches_v500_chips(self):
        for soc in V500_SOCS:
            assert HiSiliconV500.matches(soc)

    def test_no_match_standard(self):
        assert not HiSiliconV500.matches("hi3516cv300")

    def test_no_match_cv6xx(self):
        assert not HiSiliconV500.matches("hi3516cv610")


class TestV500Handshake:
    @pytest.mark.asyncio
    async def test_successful_handshake(self):
        transport = MockTransport()

        # Build a valid V500 handshake response
        chip_id = 0x12345678
        response = b"\xbd\x00\x00\x00\x00\x00\x00\x00"
        response += struct.pack(">I", chip_id)
        response += b"\x00\x00"  # padding to 14 bytes
        transport.enqueue_rx(response)

        protocol = HiSiliconV500()
        result = await protocol.handshake(transport)

        assert result.success
        assert result.chip_id == chip_id

    @pytest.mark.asyncio
    async def test_handshake_reports_chip_id(self):
        transport = MockTransport()
        chip_id = 0xAABBCCDD
        response = b"\xbd\x00" + b"\x00" * 6 + struct.pack(">I", chip_id) + b"\x00\x00"
        transport.enqueue_rx(response)

        protocol = HiSiliconV500()
        result = await protocol.handshake(transport)
        assert result.chip_id == 0xAABBCCDD


class _ScriptedRx(MockTransport):
    """Delivers one scripted RX chunk after each write, like a live UART."""

    def __init__(self, *chunks: bytes) -> None:
        super().__init__(flush_clears_buffer=True)
        self._script = list(chunks)

    async def write(self, data: bytes) -> None:
        await super().write(data)
        if self._script:
            self.enqueue_rx(self._script.pop(0))


class TestV500HandshakeCatch:
    REPLY = b"\xbd\x00" + b"\x00" * 6 + struct.pack(">I", 0x72050500) + b"\x00\x00"

    @pytest.mark.asyncio
    async def test_reply_after_boot_noise(self):
        transport = _ScriptedRx(b"", b"Starting kernel ...\r\n" + self.REPLY)
        result = await HiSiliconV500().handshake(transport)
        assert result.success
        assert result.chip_id == 0x72050500

    @pytest.mark.asyncio
    async def test_reply_split_across_reads(self):
        transport = _ScriptedRx(b"\x00\x01", self.REPLY[:1], self.REPLY[1:9], self.REPLY[9:])
        result = await HiSiliconV500().handshake(transport)
        assert result.chip_id == 0x72050500

    @pytest.mark.asyncio
    async def test_line_never_idles_between_polls(self):
        """Every write is a multi-frame burst, so the bootrom's short listen
        window cannot fall into a gap between frames."""
        transport = _ScriptedRx(b"", b"", self.REPLY)
        await HiSiliconV500().handshake(transport)
        assert len(transport.tx_log) == 3
        for burst in transport.tx_log:
            assert len(burst) >= 8 * 14
            assert burst[:4] == b"\xbd\x00\xff\x01"

    @pytest.mark.asyncio
    async def test_stale_replies_flushed(self):
        """Replies to the rest of the burst must not linger as fake ACKs."""
        transport = _ScriptedRx(self.REPLY * 3)
        await HiSiliconV500().handshake(transport)
        assert await transport.bytes_waiting() == 0


class TestV500FirmwareTransfer:
    @pytest.mark.asyncio
    async def test_send_firmware_with_acks(self):
        transport = MockTransport()

        # Build minimal V500 firmware with AUX size at offset 1024
        firmware = bytearray(32768)
        struct.pack_into("<I", firmware, 1024, 4096)  # AUX size = 4096

        # Lots of ACKs for all frames
        transport.enqueue_rx(ACK_BYTE * 500)

        protocol = HiSiliconV500()
        result = await protocol.send_firmware(transport, bytes(firmware))

        assert result.success
        assert Stage.HEAD_AREA in result.stages_completed
        assert Stage.AUX_AREA in result.stages_completed
        assert Stage.BOOT_IMAGE in result.stages_completed
