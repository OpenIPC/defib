"""Textual TUI application for defib."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from defib.tui.screens.main import MainScreen
from defib.tui.screens.progress import ProgressScreen


class DefibApp(App[None]):
    """defib - Universal Camera Recovery Tool."""

    TITLE = "defib"
    SUB_TITLE = "Shocking dead devices back to life"
    CSS = """
    Screen {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
    ]

    SCREENS = {"main": MainScreen}

    def on_mount(self) -> None:
        self.push_screen("main")

    def start_recovery(
        self, chip: str, firmware_path: str, port: str, send_break: bool
    ) -> None:
        """Switch to progress screen and begin recovery."""
        screen = ProgressScreen(chip, firmware_path, port, send_break)
        self.push_screen(screen)


def run_tui() -> None:
    """Entry point for `defib tui`."""
    app = DefibApp()
    app.run()
