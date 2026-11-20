"""Tests for the V500 boot protocol."""

import struct

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.hisilicon_v500 import (
    HiSiliconV500,
    V500_CHIP_IDS,
    V500_SOCS,
    v500_member,
)
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

    def test_gk7206_registered(self):
        # GK7206 shares the V500 bootrom protocol — verified on hardware, the
        # ROM answered the handshake with chip id 0x72060110.
        assert "gk7206" in V500_SOCS
        assert HiSiliconV500.matches("gk7206")
        assert V500_CHIP_IDS[0x72060110] == "7206"
        assert v500_member("gk7206") == "7206"


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


class TestV500HandshakeOverSocket:
    @pytest.mark.asyncio
    async def test_reply_reaches_handshake_over_socket(self):
        """tcp:// and socket:// transports only count already-received bytes
        in bytes_waiting(), so the handshake must read, not poll."""
        import asyncio
        import socket

        from defib.transport.socket import SocketTransport

        ours, bootrom = socket.socketpair()
        bootrom.setblocking(False)
        reply = b"\xbd\x00" + b"\x00" * 6 + struct.pack(">I", 0x72050510) + b"\x00\x00"
        loop = asyncio.get_running_loop()

        async def fake_bootrom() -> None:
            await loop.sock_recv(bootrom, 4096)  # first burst arrives
            await loop.sock_sendall(bootrom, reply)
            while True:  # keep swallowing the rest of the flood
                if not await loop.sock_recv(bootrom, 4096):
                    return

        peer = asyncio.create_task(fake_bootrom())
        transport = SocketTransport(ours)
        try:
            result = await asyncio.wait_for(HiSiliconV500().handshake(transport), 5)
        finally:
            await transport.close()
            bootrom.close()
            peer.cancel()
        assert result.chip_id == 0x72050510


class TestV500FirmwareTransfer:
    @pytest.mark.asyncio
    async def test_send_firmware_with_acks(self):
        transport = MockTransport()

        # Minimal V500 firmware: aux 0x1000, entry 0x40704000 -> boot at file
        # 0x4000, so key/HEAD 0x3000 and aux 0x3000..0x4000, all within 0x8000.
        firmware = bytearray(32768)
        struct.pack_into("<6I", firmware, 0x400, 0x1000, 0x800, 0xa00,
                         0x12345678, 0x12345678, 0x40704000)

        # Lots of ACKs for all frames
        transport.enqueue_rx(ACK_BYTE * 500)

        protocol = HiSiliconV500()
        result = await protocol.send_firmware(transport, bytes(firmware))

        assert result.success
        assert Stage.HEAD_AREA in result.stages_completed
        assert Stage.AUX_AREA in result.stages_completed
        assert Stage.BOOT_IMAGE in result.stages_completed


def _donor(aux_len: int = 0x5000, code_len: int = 0x800,
           entry: int = 0x40707000) -> bytes:
    """A V500 boot image.  ``entry`` (the header's load-address field) fixes
    the boot-code file offset = entry - 0x40700000; the key/HEAD area is then
    boot_off - aux_len.  Defaults model gk7205v5x0 (key 0x2000, boot 0x7000);
    pass entry=0x40706000, aux_len=0x3800 to model gk7206 (key 0x2800)."""
    from defib.protocol.hisilicon_v500 import V500_BOOT_TAIL_LEN, V500_IMAGE_BASE

    boot_off = entry - V500_IMAGE_BASE
    total = boot_off + code_len + V500_BOOT_TAIL_LEN
    image = bytearray(b"\xa5" * total)
    struct.pack_into("<6I", image, 0x400, aux_len, code_len,
                     code_len + V500_BOOT_TAIL_LEN, 0x12345678, 0x12345678, entry)
    return bytes(image)


class TestWrapV500Payload:
    def test_layout(self):
        from defib.protocol.hisilicon_v500 import V500_AGENT_LOAD_ADDR, wrap_v500_payload

        donor = _donor()
        agent = b"\x00\x00\x00\xea" + b"\x11" * 1000
        image = wrap_v500_payload(donor, agent, V500_AGENT_LOAD_ADDR["v500"])

        # Header, params and aux (DDR init) area come from the donor.
        assert image[:0x400] == donor[:0x400]
        assert image[0x40c:0x7000] == donor[0x40c:0x7000]
        # Boot code is the agent, padded to 1 KiB, followed by the zero tail.
        assert image[0x7000:0x7000 + len(agent)] == agent
        assert len(image) == 0x7000 + 0x400 + 0x200
        assert image[0x7000 + len(agent):] == b"\x00" * (len(image) - 0x7000 - len(agent))
        aux, code, total = struct.unpack_from("<3I", image, 0x400)
        assert (aux, code, total) == (0x5000, 0x400, 0x600)

    def test_load_address_must_match_code_offset(self):
        from defib.protocol.hisilicon_v500 import wrap_v500_payload

        with pytest.raises(ValueError, match="linked at 0x40707000"):
            wrap_v500_payload(_donor(), b"\x00" * 16, 0x40707000)
        # A gk7206-shaped donor (entry 0x40706000) runs its boot code at
        # 0x41006000, so a gk7205v5x0 agent linked at 0x41007000 no longer fits.
        with pytest.raises(ValueError, match="runs at 0x41006000"):
            wrap_v500_payload(
                _donor(aux_len=0x3800, entry=0x40706000), b"\x00" * 16, 0x41007000,
            )

    @pytest.mark.parametrize("aux_len", [0, 0x123, 0x100000])
    def test_rejects_implausible_donor(self, aux_len):
        from defib.protocol.hisilicon_v500 import wrap_v500_payload

        donor = bytearray(_donor())
        struct.pack_into("<I", donor, 0x400, aux_len)
        with pytest.raises(ValueError, match="not plausible"):
            wrap_v500_payload(bytes(donor), b"\x00" * 16, 0x41007000)

    def test_rejects_short_donor(self):
        from defib.protocol.hisilicon_v500 import wrap_v500_payload

        with pytest.raises(ValueError, match="too short"):
            wrap_v500_payload(b"\x00" * 0x100, b"\x00", 0x41007000)

    @pytest.mark.asyncio
    async def test_wrapped_image_sends_patched_header(self):
        """HEAD carries the patched lengths, so the bootrom loads only the agent."""
        from defib.protocol.hisilicon_v500 import V500_AGENT_LOAD_ADDR, wrap_v500_payload

        image = wrap_v500_payload(_donor(), b"\x22" * 2048, V500_AGENT_LOAD_ADDR["v500"])
        transport = MockTransport()
        transport.enqueue_rx(ACK_BYTE * 500)
        result = await HiSiliconV500().send_firmware(transport, image)
        assert result.success
        sent = transport.all_tx_data
        # The BOOT stage HEAD frame announces the wrapped size at 0x41000000.
        assert b"\xfe\x00\xff\x01" + struct.pack(">II", len(image), 0x41000000) in sent


class TestV500ImageLayout:
    """The key/aux boundaries are derived from the header, not assumed, because
    gk7205v5x0 and gk7206 differ (key 0x2000 vs 0x2800)."""

    def test_v5x0_layout(self):
        from defib.protocol.hisilicon_v500 import v500_image_layout
        # entry 0x40707000, aux 0x5000 -> key 0x2000, aux 0x2000..0x7000.
        assert v500_image_layout(_donor()) == (0x2000, 0x5000, 0x7000)

    def test_gk7206_layout(self):
        from defib.protocol.hisilicon_v500 import v500_image_layout
        # entry 0x40706000, aux 0x3800 -> key 0x2800, aux 0x2800..0x6000.
        donor = _donor(aux_len=0x3800, entry=0x40706000)
        assert v500_image_layout(donor) == (0x2800, 0x3800, 0x6000)

    def test_gk7206_wrap_keeps_head_and_aux_places_agent_at_0x6000(self):
        from defib.protocol.hisilicon_v500 import wrap_v500_payload
        donor = _donor(aux_len=0x3800, entry=0x40706000)
        agent = b"\x00\x00\x00\xea" + b"\x33" * 500
        image = wrap_v500_payload(donor, agent, 0x41006000)
        # Everything up to the boot code (key + aux) is the donor, verbatim
        # except the two patched length fields at 0x404/0x408.
        assert image[:0x404] == donor[:0x404]
        assert image[0x40c:0x6000] == donor[0x40c:0x6000]
        assert image[0x6000:0x6000 + len(agent)] == agent

    @pytest.mark.asyncio
    async def test_gk7206_send_frames_head_sized_to_key_area(self):
        """HEAD is the 0x2800 key area (not a fixed 8 KiB) and AUX lands at 0x2800."""
        from defib.protocol.hisilicon_v500 import wrap_v500_payload
        donor = _donor(aux_len=0x3800, entry=0x40706000)
        image = wrap_v500_payload(donor, b"\x44" * 512, 0x41006000)
        transport = MockTransport()
        transport.enqueue_rx(ACK_BYTE * 500)
        assert (await HiSiliconV500().send_firmware(transport, image)).success
        sent = transport.all_tx_data
        # HEAD stage announces 0x2800 bytes at address 0; AUX at address 0x2800.
        assert b"\xfe\x00\xff\x01" + struct.pack(">II", 0x2800, 0) in sent
        assert b"\xfe\x00\xff\x01" + struct.pack(">II", 0x3800, 0x2800) in sent
