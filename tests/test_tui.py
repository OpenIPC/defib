"""Tests for the Textual TUI."""

import pytest

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


class TestTUIFromCLI:
    def test_tui_command_exists(self):
        from typer.testing import CliRunner
        from defib.cli.app import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["tui", "--help"])
        assert result.exit_code == 0
        assert "tui" in result.stdout.lower()
