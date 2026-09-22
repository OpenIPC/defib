import asyncio
import zlib

import pytest

from defib.install.firmware import uboot_tftp_commands
from defib.uboot_tftp import run_uboot_tftp
from defib.install.layout import (
    align_up,
    detect_nor_size_mb,
    erased_region_crc,
    nor_layout,
    nor_mtdparts,
    parse_uboot_crc32,
    select_nor_size_mb,
    set_uboot_env_verified,
    uboot_flash_command_error,
    uboot_sf_lock_unsupported,
    verify_spi_environment_crc,
)


def test_generic_tftp_keeps_explicit_ram_address():
    assert uboot_tftp_commands("k", 0x82000000, use_loadaddr=False) == (
        "tftpboot 0x82000000 k",
        "tftp 0x82000000 k",
    )


def test_vendor_tftp_uses_verified_loadaddr_for_short_command():
    assert uboot_tftp_commands("k", 0x82000000, use_loadaddr=True) == (
        "tftpboot k",
        "tftp k",
    )


def test_shared_tftp_runner_falls_back_from_tftpboot():
    calls: list[tuple[str, float]] = []

    async def run_command(command: str, timeout: float) -> tuple[bool, str]:
        calls.append((command, timeout))
        if command.startswith("tftpboot "):
            return False, "Unknown command 'tftpboot'\nOpenIPC # "
        return True, "Bytes transferred = 1024\nOpenIPC # "

    response = asyncio.run(
        run_uboot_tftp(
            run_command,
            "k",
            0x82000000,
            use_loadaddr=False,
            timeout=17.0,
        )
    )

    assert "Bytes transferred = 1024" in response
    assert calls == [
        ("tftpboot 0x82000000 k", 17.0),
        ("tftp 0x82000000 k", 17.0),
    ]


def test_shared_tftp_runner_preserves_non_fallback_failure_status():
    async def run_command(command: str, timeout: float) -> tuple[bool, str]:
        return False, "network timeout"

    with pytest.raises(RuntimeError, match="failed or timed out"):
        asyncio.run(
            run_uboot_tftp(
                run_command,
                "k",
                0x82000000,
                use_loadaddr=False,
            )
        )



def test_shared_tftp_runner_ignores_unrelated_unknown_command():
    calls: list[str] = []

    async def run_command(command: str, timeout: float) -> tuple[bool, str]:
        calls.append(command)
        return False, "Unknown command 'crc32'\n"

    with pytest.raises(RuntimeError, match="failed or timed out"):
        asyncio.run(
            run_uboot_tftp(
                run_command,
                "k",
                0x82000000,
                use_loadaddr=False,
            )
        )

    assert calls == ["tftpboot 0x82000000 k"]


def test_align_up_for_nand_page_write():
    assert align_up(0x12345, 2048) == 0x12800


def test_align_up_rejects_invalid_alignment():
    with pytest.raises(ValueError):
        align_up(1, 0)




def test_detect_nor_size_from_hisilicon_sf_probe_formats():
    assert detect_nor_size_mb('Spi(cs1): Block:64KB Chip:16MB Name:"GD25Q128"') == 16
    assert detect_nor_size_mb("spi size: 16MB") == 16
    assert detect_nor_size_mb("SPI Nor total size: 16MB") == 16
    assert detect_nor_size_mb("SF: Detected GD25Q128 with total 16MB") == 16
    assert detect_nor_size_mb("16384 KiB hi_sfc at 0:0 is now current device") == 16
    assert detect_nor_size_mb("unhelpful output") is None


def test_parse_uboot_crc32_requires_complete_checksum():
    assert parse_uboot_crc32("CRC32 for 82000000 ... ==> DEADBEEF\nOpenIPC # ") == 0xDEADBEEF
    assert parse_uboot_crc32("CRC32 command timed out") is None
    assert parse_uboot_crc32("==> 1234") is None


def test_standard_nor_layout_is_selected_from_detected_capacity():
    assert nor_layout(8)["kernel"] == (0x050000, 0x200000)
    assert nor_layout(16)["rootfs"] == (0x350000, 0xA00000)
    assert nor_layout(32)["rootfs"] == (0x350000, 0x1800000)


def test_nor_size_override_wins_over_conflicting_detection():
    assert select_nor_size_mb(8, 16, require_detection=True) == (8, "override")


def test_stock_nor_size_requires_detection_without_override():
    with pytest.raises(ValueError, match="specify --nor-size"):
        select_nor_size_mb(0, None, require_detection=True)


def test_generic_nor_size_keeps_historic_8m_fallback():
    assert select_nor_size_mb(0, None, require_detection=False) == (8, "fallback")

def test_standard_openipc_nor_mtdparts():
    assert nor_mtdparts(8) == (
        "hi_sfc:256k(boot),64k(env),2048k(kernel),5120k(rootfs),-(rootfs_data)"
    )
    assert nor_mtdparts(16) == (
        "hi_sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)"
    )
    assert nor_mtdparts(32) == (
        "hi_sfc:256k(boot),64k(env),3072k(kernel),24576k(rootfs),-(rootfs_data)"
    )

def test_uboot_flash_error_detects_hisilicon_alignment_error():
    response = "ERROR: erase length is not block aligned!\n\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_detects_spi_write_failure():
    response = "SPI flash write failed\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_detects_missing_spi_probe():
    response = "No SPI flash selected. Please run `sf probe'\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_detects_failed_to_initialize_probe():
    response = "Failed to initialize SPI flash at 0:0 (error -2)\nOpenIPC # "
    assert uboot_flash_command_error(response) is not None


def test_sf_lock_usage_without_lock_entry_is_unsupported():
    response = (
        "Usage:\n"
        "sf probe [[bus:]cs] [hz] [mode]\n"
        "sf read addr offset len\n"
        "sf write addr offset len\n"
        "OpenIPC # "
    )
    assert uboot_sf_lock_unsupported(response) is True


def test_sf_lock_usage_with_lock_entry_is_not_unsupported():
    response = "Usage:\nsf lock [offset] [len]\nOpenIPC # "
    assert uboot_sf_lock_unsupported(response) is False
    assert uboot_flash_command_error(response) is not None


def test_uboot_flash_error_accepts_successful_progress():
    response = "Erasing at 0x240000 -- 100% complete.\nOpenIPC # "
    assert uboot_flash_command_error(response) is None


def test_uboot_flash_error_ignores_unrelated_banner_failure_text():
    response = (
        "warning: failed to read optional otp calibration\n"
        "Erasing at 0x240000 -- 100% complete.\n"
        "OpenIPC # "
    )
    assert uboot_flash_command_error(response) is None


def test_erased_region_crc_matches_direct_crc():
    for size in (0, 1, 0x10000, 0x2B0000):
        assert erased_region_crc(size) == (zlib.crc32(b"\xff" * size) & 0xFFFFFFFF)


def test_runtime_tftp_env_is_read_back_and_retried_until_exact():
    calls: list[str] = []
    reads = iter(
        [
            "serverip=192.168.1.254\nOpenIPC # ",
            "serverip=192.168.1.11\nOpenIPC # ",
        ]
    )

    async def cmd(command: str, timeout: float = 0.0) -> str:
        calls.append(command)
        if command.startswith("printenv serverip"):
            return next(reads)
        return "OpenIPC # "

    asyncio.run(set_uboot_env_verified(cmd, "serverip", "192.168.1.11"))
    assert calls == [
        "setenv serverip 192.168.1.11",
        "printenv serverip",
        "setenv serverip 192.168.1.11",
        "printenv serverip",
    ]


def test_runtime_tftp_env_refuses_stale_serverip():
    async def cmd(command: str, timeout: float = 0.0) -> str:
        if command.startswith("printenv serverip"):
            return "serverip=192.168.1.254\nOpenIPC # "
        return "OpenIPC # "

    with pytest.raises(RuntimeError, match="serverip"):
        asyncio.run(
            set_uboot_env_verified(
                cmd, "serverip", "192.168.1.11", attempts=2
            )
        )


def test_persisted_spi_environment_crc_is_read_back_and_checked():
    calls: list[str] = []

    async def cmd(command: str, timeout: float = 0.0) -> str:
        calls.append(command)
        if command == "sf probe 0":
            return "16384 KiB hi_sfc at 0:0 is now current device\nOpenIPC # "
        if command.startswith("sf read "):
            return "Read OK\nOpenIPC # "
        if command.startswith("crc32 "):
            return "CRC32 for 82000004 ... 8200ffff ==> A1B2C3D4\nOpenIPC # "
        if command.startswith("cmp.l "):
            return "Total of 1 word(s) were the same\nOpenIPC # "
        raise AssertionError(command)

    crc = asyncio.run(
        verify_spi_environment_crc(
            cmd,
            env_off=0x40000,
            env_size=0x10000,
            ram_addr=0x82000000,
        )
    )
    assert crc == 0xA1B2C3D4
    assert calls == [
        "sf probe 0",
        "sf read 0x82000000 0x40000 0x10000",
        "crc32 0x82000004 0xfffc 0x82010000",
        "cmp.l 0x82000000 0x82010000 1",
    ]


def test_persisted_spi_environment_crc_fails_fast_without_spi_probe():
    calls: list[str] = []

    async def cmd(command: str, timeout: float = 0.0) -> str:
        calls.append(command)
        if command == "sf probe 0":
            return "No SPI flash selected. Please run `sf probe'\nOpenIPC # "
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="environment SPI probe failed"):
        asyncio.run(
            verify_spi_environment_crc(
                cmd,
                env_off=0x40000,
                env_size=0x10000,
                ram_addr=0x82000000,
            )
        )
    assert calls == ["sf probe 0"]


def test_persisted_spi_environment_crc_rejects_invalid_header():
    async def cmd(command: str, timeout: float = 0.0) -> str:
        if command == "sf probe 0":
            return "16384 KiB hi_sfc at 0:0 is now current device\nOpenIPC # "
        if command.startswith("sf read "):
            return "Read OK\nOpenIPC # "
        if command.startswith("crc32 "):
            return "==> A1B2C3D4\nOpenIPC # "
        if command.startswith("cmp.l "):
            return "word at 0x82000000 (deadbeef) != (a1b2c3d4)\nOpenIPC # "
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="on-flash CRC"):
        asyncio.run(
            verify_spi_environment_crc(
                cmd,
                env_off=0x40000,
                env_size=0x10000,
                ram_addr=0x82000000,
            )
        )
