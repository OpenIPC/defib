"""Tests for the CV6xx boot protocol."""

import struct

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.protocol.frames import CV6XX_GSL_MAGIC, CV6XX_DDR_PARAMS_MAGIC, CV6XX_UBOOT_MAGIC
from defib.protocol.hisilicon_cv6xx import (
    CV6XX_SOCS,
    HiSiliconCV6xx,
    parse_cv6xx_boot,
    build_ddr_table,
)
from defib.protocol.base import ProtocolError
from defib.recovery.events import Stage
from defib.transport.mock import MockTransport


class TestCV6xxMatches:
    def test_matches_cv6xx_chips(self):
        for soc in CV6XX_SOCS:
            assert HiSiliconCV6xx.matches(soc)

    def test_no_match_standard(self):
        assert not HiSiliconCV6xx.matches("hi3516cv300")

    def test_no_match_v500(self):
        assert not HiSiliconCV6xx.matches("gk7205v500")


def _build_cv6xx_firmware(
    gsl_len: int = 4096,
    table_count: int = 2,
    table_size: int = 1024,
    uboot_len: int = 8192,
) -> bytes:
    """Build a synthetic CV6xx composite boot file for testing."""
    # GSL section starts at offset 2048
    gsl_size = gsl_len + 3072
    data = bytearray(gsl_size + 4096 + table_count * table_size + uboot_len + 4096)

    # GSL magic at offset 2048
    struct.pack_into("<I", data, 2048, CV6XX_GSL_MAGIC)
    # GSL length at offset 2084
    struct.pack_into("<I", data, 2084, gsl_len)

    # DDR params section
    params_start = gsl_size + 1024
    struct.pack_into("<I", data, params_start, CV6XX_DDR_PARAMS_MAGIC)
    # offset_32 at +32
    struct.pack_into("<I", data, params_start + 32, 0)
    # table_size at +36
    struct.pack_into("<I", data, params_start + 36, table_size)
    # table_count at +40
    struct.pack_into("<I", data, params_start + 40, table_count)
    # board_mapping at +300 (8 bytes)
    for i in range(8):
        data[params_start + 300 + i] = min(i, table_count - 1)

    # U-Boot section after params + header area + tables
    uboot_offset = gsl_size + 2048 + 2048  # After header + some padding
    # Make sure it's findable after params_start
    if uboot_offset <= params_start:
        uboot_offset = params_start + 1024

    # Expand data if needed
    needed = uboot_offset + uboot_len + 1024 + 40
    if len(data) < needed:
        data.extend(b"\x00" * (needed - len(data)))

    struct.pack_into("<I", data, uboot_offset, CV6XX_UBOOT_MAGIC)
    struct.pack_into("<I", data, uboot_offset + 36, uboot_len)

    return bytes(data)


class TestParseCv6xxBoot:
    def test_parse_valid_firmware(self):
        firmware = _build_cv6xx_firmware()
        parts = parse_cv6xx_boot(firmware)

        assert len(parts.gsl_data) > 0
        assert parts.table_count == 2
        assert parts.table_size == 1024
        assert len(parts.uboot_data) > 0

    def test_invalid_gsl_magic(self):
        firmware = bytearray(_build_cv6xx_firmware())
        struct.pack_into("<I", firmware, 2048, 0xDEADBEEF)

        with pytest.raises(ProtocolError, match="Invalid GSL magic"):
            parse_cv6xx_boot(firmware)

    def test_build_ddr_table(self):
        firmware = _build_cv6xx_firmware(table_count=3, table_size=512)
        parts = parse_cv6xx_boot(firmware)
        ddr_table = build_ddr_table(parts, board_id=0)
        assert isinstance(ddr_table, bytes)
        assert len(ddr_table) > 0


class TestCV6xxHandshake:
    @pytest.mark.asyncio
    async def test_successful_handshake(self):
        transport = MockTransport()
        # Simulate device responding with "uart ddr" in the stream
        transport.enqueue_rx(b"\x00\x00uart ddr ready\r\n")

        protocol = HiSiliconCV6xx()
        result = await protocol.handshake(transport)

        assert result.success
        assert "handshake complete" in result.message.lower()


class TestCV6xxFirmwareTransfer:
    @pytest.mark.asyncio
    async def test_send_firmware(self):
        """Test CV6xx firmware transfer.

        The mock transport feeds bytes sequentially. The protocol flow is:
        1. GSL transfer (HEAD + DATA chunks + TAIL, each needs ACK)
        2. Board ID query (flush_input clears buffer, writes query, reads response)
        3. DDR table transfer (HEAD + DATA + TAIL with ACKs)
        4. DDR training (reads ASCII output)
        5. U-Boot transfer (HEAD + DATA + TAIL with ACKs)

        We use a ScriptedMockTransport that returns different data based on
        what was written, but for simplicity just ensure enough ACKs are
        available at each stage.
        """
        # Use flush_clears_buffer=False so flush_input() between stages
        # doesn't wipe the pre-loaded response buffer
        transport = MockTransport(flush_clears_buffer=False)
        firmware = _build_cv6xx_firmware()

        # Board ID response: CE + cpu_id + padding + board_id(4B) + padding + AA
        board_response = bytearray(11)
        board_response[0] = 0xCE
        board_response[1] = 0x01  # CPU ID
        struct.pack_into(">I", board_response, 4, 0)  # Board ID = 0
        board_response[10] = 0xAA

        # All data goes into one sequential buffer.
        # GSL: parse to see how many chunks
        parts = parse_cv6xx_boot(firmware)
        gsl_chunks = (len(parts.gsl_data) + 1023) // 1024
        gsl_acks = 1 + gsl_chunks + 1  # HEAD + DATA chunks + TAIL

        # Pre-load all responses. _get_board_id reads all available bytes
        # at once, so we need to ensure DDR/U-Boot ACKs survive that bulk read.
        # Solution: load all data into one big buffer. _get_board_id will
        # consume the board_response + some extra ACKs, so we pad generously.
        transport.enqueue_rx(ACK_BYTE * gsl_acks)

        # Board ID response
        transport.enqueue_rx(bytes(board_response))

        # After board_id query reads everything available, the DDR transfer
        # will need ACKs. Since board_id might consume extra, load plenty.
        transport.enqueue_rx(ACK_BYTE * 2000)

        # DDR training output (will be consumed during the 1.5s wait)
        transport.enqueue_rx(b"DDR training OK\r\n")

        # More ACKs for U-Boot transfer. The DDR training wait loop
        # also reads bytes_waiting() and may consume some ACKs, so pad generously.
        transport.enqueue_rx(ACK_BYTE * 5000)

        # Use a very short DDR training wait for fast tests
        protocol = HiSiliconCV6xx(ddr_training_wait=0.05)
        result = await protocol.send_firmware(transport, firmware)

        assert result.success
        assert Stage.GSL in result.stages_completed
