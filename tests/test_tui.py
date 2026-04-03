"""Tests for the Textual TUI."""

import pytest
from types import SimpleNamespace

from defib.tui.app import DefibApp


class TestTUIApp:
    @pytest.mark.asyncio
    async def test_app_creates(self):
        """App can be instantiated without crashing."""
        app = DefibApp()
        assert app.title == "defib"

    @pytest.mark.asyncio
    async def test_app_runs_headless(self):
        """App runs in headless mode without crashing."""
        app = DefibApp()
        async with app.run_test(size=(120, 40)):
            # Main screen should be displayed
            assert app.screen is not None

    @pytest.mark.asyncio
    async def test_main_screen_has_widgets(self):
        """Main screen contains the expected widgets."""
        app = DefibApp()
        async with app.run_test(size=(120, 40)):
            # Should have chip selector, file input, port selector, start button
            screen = app.screen
            assert screen.query_one("#chip-select") is not None
            assert screen.query_one("#file-input") is not None
            assert screen.query_one("#port-select") is not None
            assert screen.query_one("#start-btn") is not None

    def test_serial_port_options_prefer_alias_when_present(self, monkeypatch):
        from defib.tui.screens.main import _get_serial_ports

        fake_port = SimpleNamespace(
            display_name="/dev/uart-orangepi5plus -> /dev/ttyUSB1 | FTDI FT232R USB UART | loc 5-2 | ser A50285BI",
            open_path="/dev/uart-orangepi5plus",
        )
        monkeypatch.setattr("defib.tui.screens.main.list_serial_ports", lambda: [fake_port])

        options = _get_serial_ports()
        assert options == [
            (
                "/dev/uart-orangepi5plus -> /dev/ttyUSB1 | FTDI FT232R USB UART | loc 5-2 | ser A50285BI",
                "/dev/uart-orangepi5plus",
            )
        ]

    @pytest.mark.asyncio
    async def test_start_without_input_shows_error(self):
        """Pressing start without filling in fields shows validation error."""
        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            # Click start without filling anything — should not crash
            await pilot.click("#start-btn")
            await pilot.pause()

    @pytest.mark.asyncio
    async def test_quit_binding(self):
        """Pressing q should quit the app."""
        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("q")


class TestProgressScreen:
    @pytest.mark.asyncio
    async def test_progress_screen_renders(self):
        """Regression: ProgressScreen must not crash on compose.

        Previously failed with BadIdentifier because stage names like
        'DDR Init' produced ids with spaces ('stage-ddr init').
        """
        from defib.tui.screens.progress import ProgressScreen

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            screen = ProgressScreen("hi3516ev300", "/tmp/fake.bin", "/dev/ttyUSB0", False)
            app.push_screen(screen)
            await pilot.pause()
            # All 4 stage indicators should exist with valid ids
            assert screen.query_one("#stage-handshake") is not None
            assert screen.query_one("#stage-ddr-init") is not None
            assert screen.query_one("#stage-spl-gsl") is not None
            assert screen.query_one("#stage-u-boot") is not None


class TestFlashDoctorScreen:
    @pytest.mark.asyncio
    async def test_flash_doctor_screen_renders(self):
        """FlashDoctorScreen composes without crashing."""
        from defib.tui.screens.flash_doctor import FlashDoctorScreen

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.start_flash_doctor(chip="hi3516ev300", port="/dev/ttyUSB0")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FlashDoctorScreen)
            assert screen.query_one("#doctor-banner") is not None
            assert screen.query_one("#sector-grid") is not None
            assert screen.query_one("#scan-stats") is not None
            assert screen.query_one("#results-log") is not None
            assert screen.query_one("#connect-scan-btn") is not None

    @pytest.mark.asyncio
    async def test_flash_doctor_blocked_without_port(self):
        """Flash Doctor button requires port selection."""
        app = DefibApp()
        async with app.run_test(size=(120, 40)):
            btn = app.screen.query_one("#doctor-btn")
            assert btn is not None

    @pytest.mark.asyncio
    async def test_flash_doctor_opens_with_chip_and_port(self):
        """Flash Doctor opens when chip and port are selected."""
        from defib.tui.screens.flash_doctor import FlashDoctorScreen

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            # Bypass UI selection — push screen directly
            app.start_flash_doctor(chip="hi3516ev300", port="/dev/ttyUSB0")
            await pilot.pause()
            assert isinstance(app.screen, FlashDoctorScreen)

    @pytest.mark.asyncio
    async def test_flash_doctor_escape_goes_back(self):
        """Pressing Escape on FlashDoctorScreen returns to main."""
        from defib.tui.screens.flash_doctor import FlashDoctorScreen
        from defib.tui.screens.main import MainScreen

        app = DefibApp()
        async with app.run_test(size=(120, 40)) as pilot:
            app.start_flash_doctor(chip="hi3516ev300", port="/dev/ttyUSB0")
            await pilot.pause()
            assert isinstance(app.screen, FlashDoctorScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)

    def test_sector_grid_render(self):
        """SectorGrid renders correct block characters for each status."""
        from defib.tui.screens.flash_doctor import SectorGrid
        from defib.agent.client import SectorStatus

        grid = SectorGrid(num_sectors=8, cols=8)
        grid._sector_size = 0x10000

        grid.set_sector(0, SectorStatus.GOOD)
        grid.set_sector(1, SectorStatus.EMPTY)
        grid.set_sector(2, SectorStatus.STUCK_ZERO)
        grid.set_sector(3, SectorStatus.UNSTABLE)
        grid.set_sector(4, SectorStatus.READ_ERROR)

        rendered = grid.render()
        assert "█" in rendered  # GOOD
        assert "·" in rendered  # EMPTY
        assert "▓" in rendered  # DEAD
        assert "✕" in rendered  # ERROR
        assert "░" in rendered  # PENDING (sectors 5-7)

    def test_build_banner_alignment(self):
        """Banner lines have consistent visible width."""
        from defib.tui.screens.flash_doctor import _build_banner, BOX_INNER

        banner = _build_banner("test subtitle")
        lines = banner.split("\n")
        assert len(lines) == 4
        # Top and bottom borders should have BOX_INNER ═ chars
        assert "═" * BOX_INNER in lines[0]
        assert "═" * BOX_INNER in lines[3]

    def test_scan_stats_update(self):
        """ScanStats widget doesn't crash on update."""
        from defib.tui.screens.flash_doctor import ScanStats

        stats = ScanStats()
        # Should not raise
        stats.update_stats(128, 256, 100, 20, 5, 3, 18.5)


class TestTUIFromCLI:
    def test_tui_command_exists(self):
        from typer.testing import CliRunner
        from defib.cli.app import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["tui", "--help"])
        assert result.exit_code == 0
        assert "tui" in result.stdout.lower()
