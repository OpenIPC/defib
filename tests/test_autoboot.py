"""Regression tests for U-Boot autoboot interruption.

Based on real captures:
- defib_hi3516ev300_20260330_163002.log (autoboot NOT caught — old behavior)
- defib_hi3516ev300_20260330_163650.log (autoboot caught — console-level detection)

The boot sequence after firmware upload is:
1. Device decompresses and relocates U-Boot (several seconds)
2. U-Boot initializes hardware (SPI, NAND, MMC, network)
3. U-Boot prints autoboot countdown:
   "Press Ctrl-c to stop autoboot... 3" (OpenIPC)
   "Hit ctrl+c to stop autoboot:  3"   (stock XM)
4. If not interrupted, U-Boot loads and boots the kernel

Two levels of autoboot interruption:
1. session.run(send_break=True) — sends Ctrl-C for 15s after upload
2. Console read loop — auto-detects "autoboot" in serial stream and
   sends Ctrl-C immediately (works regardless of send_break setting)
"""

import asyncio

import pytest

from defib.protocol.crc import ACK_BYTE
from defib.recovery.session import RecoverySession
from defib.transport.mock import MockTransport

# Real boot output from hi3516ev300 (from defib_hi3516ev300_20260330_163650.log)
UBOOT_BOOT_OUTPUT = (
    b"\r\n\r\nSystem startup\r\n\r\n"
    b"Uncompress Ok!\r\n\r\n"
    b"U-Boot 2016.11-g6d2ed0c-dirty (Mar 20 2023 - 13:54:41 +0300)hi3516ev300\r\n\r\n"
    b"Relocation Offset is: 0774a000\r\n"
    b"Relocating to 47f4a000, new gd at 47b39ef0, sp at 47b39ed0\r\n"
    b"SPI Nor:  hifmc_ip_ver_check(44\r\n"
    b"): Check Flash Memory Controller v100 ...hifmc_ip_ver_check(50):  Found\r\n"
    b"hifmc_spi_nor_probe(1802): SPI Nor ID Table Version 1.0\r\n"
    b"hifmc_spi_nor_probe(1827): SPI Nor(cs 0) ID: 0xef 0x40 0x18\r\n"
    b"Block:64KB Chip:16MB Name:\"W25Q128(B/F)V\"\r\n"
    b"Spi is locked. lock address[0 => 0x800000]\r\n"
    b"hifmc100_spi_nor_probe(147): SPI Nor total size: 16MB\r\n"
    b"NAND:  0 MiB\r\n"
    b"MMC:   Card did \r\nnot respond to voltage select!\r\n"
    b"No SD device found !\r\nhisi-sdhci: 0\r\n"
    b"*** Warning - bad CRC, using default environment\r\n\r\n"
    b"In:    serial\r\nOut:   serial\r\nErr:   serial\r\n"
    b"RAM size: 128MB\r\nNet:   eth0\r\n\r\n"
)

AUTOBOOT_OPENIPC = b"Press Ctrl-c to stop autoboot... 1 \r\n"
AUTOBOOT_XM = b"Hit ctrl+c to stop autoboot:  1 \x08\x08 0 \r\n"
OPENIPC_PROMPT = b"\r\nOpenIPC # "
HISILICON_PROMPT = b"\r\nhisilicon # "
UBOOT_STANDARD_PROMPT = b"\r\n=> "


class TestSessionAutobreak:
    """Test session.run(send_break=True) autoboot interruption."""

    @pytest.mark.asyncio
    async def test_detects_openipc_autoboot(self):
        """Should detect 'Press Ctrl-c to stop autoboot' and send 0x03."""
        transport = MockTransport(flush_clears_buffer=False)

        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 500)
        transport.enqueue_rx(UBOOT_BOOT_OUTPUT)
        transport.enqueue_rx(AUTOBOOT_OPENIPC)
        transport.enqueue_rx(OPENIPC_PROMPT)

        session = RecoverySession(chip="hi3516cv300", firmware_data=bytes(range(256)) * 80)

        log_events: list[str] = []
        result = await session.run(
            transport,
            on_log=lambda e: log_events.append(e.message),
            send_break=True,
        )

        assert result.success
        assert b"\x03" in transport.all_tx_data
        assert any("autoboot" in m.lower() and "ctrl-c" in m.lower() for m in log_events)
        assert any("console ready" in m.lower() for m in log_events)

    @pytest.mark.asyncio
    async def test_detects_xm_autoboot(self):
        """Should detect 'Hit ctrl+c to stop autoboot' variant."""
        transport = MockTransport(flush_clears_buffer=False)

        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 500)
        transport.enqueue_rx(UBOOT_BOOT_OUTPUT)
        transport.enqueue_rx(AUTOBOOT_XM)
        transport.enqueue_rx(HISILICON_PROMPT)

        session = RecoverySession(chip="hi3516cv300", firmware_data=bytes(range(256)) * 80)
        log_events: list[str] = []

        result = await session.run(
            transport,
            on_log=lambda e: log_events.append(e.message),
            send_break=True,
        )

        assert result.success
        assert b"\x03" in transport.all_tx_data
        assert any("autoboot" in m.lower() and "ctrl-c" in m.lower() for m in log_events)

    @pytest.mark.asyncio
    async def test_detects_standard_prompt(self):
        """Should detect '=> ' standard U-Boot prompt."""
        transport = MockTransport(flush_clears_buffer=False)

        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 500)
        transport.enqueue_rx(UBOOT_BOOT_OUTPUT)
        transport.enqueue_rx(UBOOT_STANDARD_PROMPT)

        session = RecoverySession(chip="hi3516cv300", firmware_data=bytes(range(256)) * 80)
        log_events: list[str] = []

        result = await session.run(
            transport,
            on_log=lambda e: log_events.append(e.message),
            send_break=True,
        )

        assert result.success
        assert any("console ready" in m.lower() for m in log_events)

    @pytest.mark.asyncio
    async def test_break_not_requested(self):
        """When send_break=False, no break logic runs."""
        transport = MockTransport(flush_clears_buffer=False)

        transport.enqueue_rx(b"\x20" * 5)
        transport.enqueue_rx(ACK_BYTE * 500)

        session = RecoverySession(chip="hi3516cv300", firmware_data=bytes(range(256)) * 80)
        log_events: list[str] = []

        result = await session.run(
            transport,
            on_log=lambda e: log_events.append(e.message),
            send_break=False,
        )

        assert result.success
        # No autoboot/break related log messages
        assert not any("autoboot" in m.lower() for m in log_events)
        assert not any("waiting for u-boot" in m.lower() for m in log_events)


class TestConsoleAutobootDetection:
    """Test console-level autoboot detection (the read loop).

    Regression: defib_hi3516ev300_20260330_163650.log shows this working:
    - Line 50: "Press Ctrl-c to stop autoboot... 1"
    - Line 52: "Autoboot detected! Sending Ctrl-C..."
    - Line 53+: "OpenIPC # <INTERRUPT>" (Ctrl-C received by U-Boot)
    """

    @pytest.mark.asyncio
    async def test_console_detects_autoboot_in_stream(self):
        """Console read loop should auto-send Ctrl-C when 'autoboot' appears."""
        from defib.tui.screens.progress import ProgressScreen
        from defib.tui.app import DefibApp

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = ProgressScreen("hi3516ev300", "/tmp/fake.bin", "/dev/ttyUSB0", False)
            app.push_screen(screen)
            await pilot.pause()

            # Simulate: put screen in console mode with a mock transport
            mock = MockTransport(flush_clears_buffer=False)
            mock.enqueue_rx(UBOOT_BOOT_OUTPUT)
            mock.enqueue_rx(AUTOBOOT_OPENIPC)
            mock.enqueue_rx(OPENIPC_PROMPT)

            screen._transport = mock
            screen._console_mode = True

            # Run the read loop briefly
            task = asyncio.ensure_future(screen._console_read_loop())
            await asyncio.sleep(0.5)
            screen._console_mode = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should have sent Ctrl-C bytes (0x03) to the transport
            assert b"\x03" in mock.all_tx_data

            # Check that autoboot was logged
            log_text = "\n".join(screen._log_buffer)
            assert "autoboot detected" in log_text.lower()

    @pytest.mark.asyncio
    async def test_console_no_false_positive(self):
        """Console should NOT send Ctrl-C for normal output without 'autoboot'."""
        from defib.tui.screens.progress import ProgressScreen
        from defib.tui.app import DefibApp

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = ProgressScreen("hi3516ev300", "/tmp/fake.bin", "/dev/ttyUSB0", False)
            app.push_screen(screen)
            await pilot.pause()

            mock = MockTransport(flush_clears_buffer=False)
            mock.enqueue_rx(b"U-Boot starting...\r\nReady\r\n=> ")

            screen._transport = mock
            screen._console_mode = True

            task = asyncio.ensure_future(screen._console_read_loop())
            await asyncio.sleep(0.3)
            screen._console_mode = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should NOT have sent any Ctrl-C
            assert b"\x03" not in mock.all_tx_data
