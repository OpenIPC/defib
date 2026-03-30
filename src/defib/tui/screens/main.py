"""Main setup screen: chip selector, file picker, port selector, start button."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal, Center
from textual.screen import Screen
from textual.widgets import (
    Header,
    Footer,
    Static,
    Input,
    Button,
    Select,
    Label,
    Checkbox,
)

from defib.profiles.loader import list_all_chips


def _get_serial_ports() -> list[tuple[str, str]]:
    """Get available serial ports as (label, value) tuples."""
    try:
        from serial.tools.list_ports import comports
        ports = sorted(comports(), key=lambda p: p.device)
        if ports:
            return [(f"{p.device} - {p.description}", p.device) for p in ports]
    except Exception:
        pass
    return [("No ports found", "")]


class MainScreen(Screen[None]):
    """Setup screen for configuring the recovery session."""

    CSS = """
    MainScreen {
        layout: vertical;
        align: center middle;
    }

    #form-container {
        width: 70;
        height: auto;
        max-height: 30;
        border: thick $accent;
        padding: 1 2;
        background: $panel;
    }

    #form-container Label {
        margin-top: 1;
        color: $text;
    }

    #form-container Input, #form-container Select {
        margin-bottom: 0;
    }

    #title-label {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
        width: 100%;
    }

    #button-row {
        margin-top: 1;
        align: center middle;
        height: 3;
    }

    #start-btn {
        min-width: 20;
    }

    #chip-input {
        width: 100%;
    }

    #file-input {
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()

        chips = list_all_chips()
        chip_options = [(c, c) for c in chips]
        port_options = _get_serial_ports()

        with Center():
            with Vertical(id="form-container"):
                yield Static("⚡ defib Recovery Setup", id="title-label")

                yield Label("Chip Model:")
                yield Select(
                    chip_options,
                    prompt="Select chip...",
                    id="chip-select",
                    allow_blank=True,
                )

                yield Label("Firmware File:")
                yield Input(
                    placeholder="/path/to/u-boot.bin",
                    id="file-input",
                )

                yield Label("Serial Port:")
                yield Select(
                    port_options,
                    prompt="Select port...",
                    id="port-select",
                    allow_blank=True,
                    value=port_options[0][1] if port_options else "",
                )

                yield Checkbox("Send Ctrl-C after upload (enter U-Boot console)", id="break-check")

                with Horizontal(id="button-row"):
                    yield Button("Start Recovery", variant="primary", id="start-btn")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self._start_recovery()

    def _start_recovery(self) -> None:
        chip_select = self.query_one("#chip-select", Select)
        file_input = self.query_one("#file-input", Input)
        port_select = self.query_one("#port-select", Select)
        break_check = self.query_one("#break-check", Checkbox)

        chip = str(chip_select.value) if chip_select.value != Select.BLANK else ""
        firmware_path = file_input.value.strip()
        port = str(port_select.value) if port_select.value != Select.BLANK else ""
        send_break = break_check.value

        # Validation
        errors: list[str] = []
        if not chip:
            errors.append("Select a chip model")
        if not firmware_path:
            errors.append("Enter a firmware file path")
        elif not Path(firmware_path).is_file():
            errors.append(f"File not found: {firmware_path}")
        if not port:
            errors.append("Select a serial port")

        if errors:
            self.notify("\n".join(errors), severity="error", title="Validation Error")
            return

        # Start recovery via the app
        from defib.tui.app import DefibApp
        app = self.app
        if isinstance(app, DefibApp):
            app.start_recovery(chip, firmware_path, port, send_break)
