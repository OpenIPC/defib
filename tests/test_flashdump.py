"""Tests for flash dump via U-Boot serial console."""


from defib.flashdump import detect_flash_from_text, get_ram_staging_addr, parse_md_line, parse_md_output


class TestParseMdLine:
    def test_standard_line(self):
        line = "82000000: ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff    ................"
        result = parse_md_line(line)
        assert result is not None
        addr, data = result
        assert addr == 0x82000000
        assert data == b"\xff" * 16

    def test_mixed_data(self):
        line = "82000010: 48 69 53 69 6c 69 63 6f 6e 00 00 00 00 00 00 00    HiSilico........"
        result = parse_md_line(line)
        assert result is not None
        addr, data = result
        assert addr == 0x82000010
        assert data[:8] == b"HiSilico"

    def test_partial_line(self):
        """Last line of a dump may have fewer than 16 bytes."""
        line = "82000ff0: 01 02 03 04"
        result = parse_md_line(line)
        assert result is not None
        addr, data = result
        assert addr == 0x82000FF0
        assert data == b"\x01\x02\x03\x04"

    def test_non_data_line(self):
        assert parse_md_line("hisilicon #") is None
        assert parse_md_line("") is None
        assert parse_md_line("SF: 3145728 bytes @ 0x50000 Read: OK") is None

    def test_uboot_prompt_not_matched(self):
        assert parse_md_line("OpenIPC # ") is None

    def test_zero_data(self):
        line = "82000000: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00    ................"
        result = parse_md_line(line)
        assert result is not None
        assert result[1] == b"\x00" * 16


class TestParseMdOutput:
    def test_multiline(self):
        text = (
            "82000000: ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff    ................\n"
            "82000010: 48 69 53 69 6c 69 63 6f 6e 00 00 00 00 00 00 00    HiSilico........\n"
            "hisilicon # \n"
        )
        data = parse_md_output(text)
        assert len(data) == 32
        assert data[:16] == b"\xff" * 16
        assert data[16:24] == b"HiSilico"

    def test_with_command_echo(self):
        """md.b output typically starts with the command echo."""
        text = (
            "md.b 0x82000000 0x20\n"
            "82000000: 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f 10    ................\n"
            "82000010: 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f 20    ............... \n"
            "hisilicon # \n"
        )
        data = parse_md_output(text)
        assert len(data) == 32
        assert data[0] == 0x01
        assert data[31] == 0x20

    def test_empty_output(self):
        assert parse_md_output("") == b""
        assert parse_md_output("hisilicon #\n") == b""

    def test_gap_in_addresses(self):
        """If there's a gap, it should be filled with the data we have."""
        text = (
            "82000000: aa bb cc dd 00 00 00 00 00 00 00 00 00 00 00 00    ................\n"
            "82000020: ee ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00    ................\n"
        )
        data = parse_md_output(text)
        # Gap from 0x10 to 0x1f should be zeros
        assert len(data) == 0x30
        assert data[0] == 0xAA
        assert data[0x10:0x20] == b"\x00" * 16  # Gap filled with zeros
        assert data[0x20] == 0xEE

    def test_real_flash_header(self):
        """Test with realistic U-Boot flash content."""
        text = (
            "82000000: d1 00 00 10 00 00 00 00 00 00 00 00 00 00 00 00    ................\n"
            "82000010: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00    ................\n"
            "82000020: 55 2d 42 6f 6f 74 00 00 00 00 00 00 00 00 00 00    U-Boot..........\n"
        )
        data = parse_md_output(text)
        assert len(data) == 48
        assert data[0] == 0xD1
        assert data[0x20:0x26] == b"U-Boot"


class TestDetectFlashFromText:
    """Regression: defib_hi3516ev300_20260330_165630.log — size not detected."""

    def test_openipc_boot_log(self):
        """Real boot output: 'Chip:16MB' and 'SPI Nor total size: 16MB'."""
        boot_log = (
            "hifmc_spi_nor_probe(1892): Block:64KB "
            "hifmc_spi_nor_probe(1893): Chip:16MB "
            "hifmc_spi_nor_probe(1894): Name:\"W25Q128(B/F)V\"\n"
            "hifmc100_spi_nor_probe(147): SPI Nor total size: 16MB\n"
        )
        assert detect_flash_from_text(boot_log) == 0x1000000  # 16MB

    def test_xm_boot_log(self):
        """XM U-Boot: 'Flash Name: XM_W25Q128FV, 0x1000000'."""
        boot_log = "Flash Name: XM_W25Q128FV, W25Q128JV{0xEF4018), 0x1000000.\n"
        assert detect_flash_from_text(boot_log) == 0x1000000

    def test_8mb_flash(self):
        assert detect_flash_from_text("Chip:8MB Name:W25Q64") == 0x800000

    def test_32mb_flash(self):
        assert detect_flash_from_text("total size: 32MB") == 0x2000000

    def test_no_flash_info(self):
        assert detect_flash_from_text("U-Boot starting...\nReady\n") is None

    def test_empty(self):
        assert detect_flash_from_text("") is None

    def test_sf_probe_response(self):
        """sf probe 0 minimal response with size."""
        resp = "SF: Detected W25Q128 with page size 256, total 16MB\n"
        assert detect_flash_from_text(resp) == 0x1000000

    def test_128mb_ram_not_matched_as_8mb(self):
        """Regression: 'RAM size: 128MB' must NOT match as 8MB flash.

        The substring '8mb' appears in '128mb'. The old code used
        plain 'in' check which caused false detection.
        From: defib_hi3516ev300_20260330_170543.log
        """
        boot_log = (
            "Chip:16MB Name:\"W25Q128(B/F)V\"\n"
            "SPI Nor total size: 16MB\n"
            "RAM size: 128MB\n"
        )
        assert detect_flash_from_text(boot_log) == 0x1000000  # 16MB, not 8MB

    def test_ram_128mb_alone_no_match(self):
        """RAM size alone should not be detected as flash size."""
        assert detect_flash_from_text("RAM size: 128MB\n") is None


class TestGetRamStagingAddr:
    """Regression: md.b 0x82000000 caused data abort on hi3516ev300.

    The RAM base is 0x40000000 on ev200/ev300 chips, not 0x80000000.
    Using 0x82000000 crashes the CPU.
    """

    def test_hi3516ev300_ram_at_0x40(self):
        addr = get_ram_staging_addr("hi3516ev300")
        assert addr >= 0x40000000
        assert addr < 0x50000000

    def test_hi3516cv300_ram_at_0x80(self):
        addr = get_ram_staging_addr("hi3516cv300")
        assert addr >= 0x80000000
        assert addr < 0x90000000

    def test_gk7205v200_ram_at_0x40(self):
        addr = get_ram_staging_addr("gk7205v200")
        assert addr >= 0x40000000
        assert addr < 0x50000000

    def test_hi3518ev200_ram_at_0x80(self):
        addr = get_ram_staging_addr("hi3518ev200")
        assert addr >= 0x80000000
        assert addr < 0x90000000

    def test_hi3516cv610_ram_at_0x40(self):
        addr = get_ram_staging_addr("hi3516cv610")
        assert addr >= 0x40000000
        assert addr < 0x50000000


class TestCrc32Detection:
    """Test CRC32 command detection and parsing."""

    def test_parse_crc32_response(self):
        """U-Boot crc32 output: '... ==> abcd1234'."""
        import re
        resp = "CRC32 for 42000000 ... 42000fff ==> 1a2b3c4d\nOpenIPC # "
        m = re.search(r"==>\s*([0-9a-fA-F]{8})", resp)
        assert m is not None
        assert int(m.group(1), 16) == 0x1A2B3C4D

    def test_parse_crc32_no_match(self):
        """Unknown command response has no ==> pattern."""
        import re
        resp = "Unknown command 'crc32'\nOpenIPC # "
        m = re.search(r"==>\s*([0-9a-fA-F]{8})", resp)
        assert m is None

    def test_detect_unknown_command(self):
        """'Unknown command' in response means crc32 not available."""
        resp = "Unknown command 'crc32' - try 'help'\nOpenIPC # "
        assert "unknown command" in resp.lower()
