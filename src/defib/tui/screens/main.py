"""Main setup screen: chip selector, firmware (auto-download or local), port selector."""

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

from defib.firmware import has_firmware, download_firmware, get_cached_path
from defib.profiles.loader import list_all_chips


def _get_serial_ports() -> list[tuple[str, str]]:
    """Get available serial ports as (label, value) tuples."""
    try:
        from serial.tools.list_ports import comports
        ports = sorted(
            [p for p in comports() if p.vid is not None],
            key=lambda p: p.device,
        )
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
        width: 74;
        height: auto;
        max-height: 34;
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

    #fw-status {
        height: 1;
        color: $success;
        margin-top: 0;
    }

    #fw-hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
    }

    #button-row {
        margin-top: 1;
        align: center middle;
        height: 3;
    }

    #start-btn {
        min-width: 20;
    }

    #doctor-btn {
        min-width: 20;
        margin-left: 2;
    }

    #download-btn {
        min-width: 30;
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

                yield Label("Firmware:")
                yield Button(
                    "Select a chip first",
                    variant="default",
                    id="download-btn",
                    disabled=True,
                )
                yield Static("", id="fw-status")
                yield Static("", id="fw-hint")
                yield Input(
                    placeholder="Or enter path to local firmware file",
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

                yield Checkbox(
                    "Send Ctrl-C after upload (enter U-Boot console)",
                    id="break-check",
                )

                with Horizontal(id="button-row"):
                    yield Button("Start Recovery", variant="primary", id="start-btn")
                    yield Button("Flash Doctor", variant="warning", id="doctor-btn")

        yield Footer()

    def _get_chip(self) -> str:
        sel = self.query_one("#chip-select", Select)
        return str(sel.value) if isinstance(sel.value, str) else ""

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "chip-select":
            self._on_chip_changed()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "file-input":
            self._update_start_button()

    def _on_chip_changed(self) -> None:
        chip = self._get_chip()
        dl_btn = self.query_one("#download-btn", Button)
        hint = self.query_one("#fw-hint", Static)
        status = self.query_one("#fw-status", Static)

        if not chip:
            dl_btn.label = "Select a chip first"
            dl_btn.disabled = True
            dl_btn.variant = "default"
            hint.update("")
            status.update("")
        elif has_firmware(chip):
            cached = get_cached_path(chip)
            if cached:
                dl_btn.label = f"Re-download U-Boot for {chip}"
                dl_btn.disabled = False
                dl_btn.variant = "default"
                status.update(f"✓ Cached: {cached.name} ({cached.stat().st_size // 1024} KB)")
            else:
                dl_btn.label = f"Download U-Boot for {chip}"
                dl_btn.disabled = False
                dl_btn.variant = "success"
                status.update("")
            hint.update("Or enter a local file path below for custom builds.")
        else:
            dl_btn.label = "No OpenIPC build available"
            dl_btn.disabled = True
            dl_btn.variant = "default"
            hint.update("Enter a local firmware file path below.")
            status.update("")

        self._update_start_button()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self._start_recovery()
        elif event.button.id == "download-btn":
            self._download_firmware()
        elif event.button.id == "doctor-btn":
            self._start_flash_doctor()

    def _download_firmware(self) -> None:
        chip = self._get_chip()
        if not chip or not has_firmware(chip):
            return

        dl_btn = self.query_one("#download-btn", Button)
        status = self.query_one("#fw-status", Static)

        dl_btn.label = "Downloading..."
        dl_btn.disabled = True
        status.update("")

        try:
            path = download_firmware(chip)
            status.update(f"✓ {path.name} ({path.stat().st_size // 1024} KB)")
            dl_btn.label = f"Re-download U-Boot for {chip}"
            dl_btn.disabled = False
            dl_btn.variant = "default"
            self.notify(
                f"Downloaded {path.name}",
                severity="information",
                title="Firmware Ready",
            )
        except (ValueError, ConnectionError) as e:
            status.update("")
            dl_btn.label = "Download failed — retry?"
            dl_btn.disabled = False
            dl_btn.variant = "error"
            self.notify(str(e), severity="error", title="Download Failed")

        self._update_start_button()

    def _get_firmware_path(self) -> str:
        """Get firmware path: local file input takes priority, then cached download."""
        local = str(self.query_one("#file-input", Input).value).strip()
        if local:
            return local

        chip = self._get_chip()
        if chip:
            cached = get_cached_path(chip)
            if cached:
                return str(cached)

        return ""

    def _update_start_button(self) -> None:
        chip = self._get_chip()
        firmware = self._get_firmware_path()
        port_sel = self.query_one("#port-select", Select)
        port = str(port_sel.value) if port_sel.value != Select.BLANK else ""

        self.query_one("#start-btn", Button).disabled = not (chip and firmware and port)

    def _start_recovery(self) -> None:
        chip = self._get_chip()
        firmware_path = self._get_firmware_path()
        port_sel = self.query_one("#port-select", Select)
        port = str(port_sel.value) if port_sel.value != Select.BLANK else ""
        break_check = self.query_one("#break-check", Checkbox)
        send_break = break_check.value

        # Validation
        errors: list[str] = []
        if not chip:
            errors.append("Select a chip model")
        if not firmware_path:
            errors.append("Download firmware or enter a file path")
        elif not Path(firmware_path).is_file():
            errors.append(f"File not found: {firmware_path}")
        if not port:
            errors.append("Select a serial port")

        if errors:
            self.notify("\n".join(errors), severity="error", title="Validation Error")
            return

        from defib.tui.app import DefibApp
        app = self.app
        if isinstance(app, DefibApp):
            app.start_recovery(chip, firmware_path, port, send_break)

    def _start_flash_doctor(self) -> None:
        chip = self._get_chip()
        port_sel = self.query_one("#port-select", Select)
        port = str(port_sel.value) if isinstance(port_sel.value, str) else ""

        errors: list[str] = []
        if not chip:
            errors.append("Select a chip model")
        if not port:
            errors.append("Select a serial port")
        if errors:
            self.notify("\n".join(errors), severity="error", title="Flash Doctor")
            return

        from defib.tui.app import DefibApp
        app = self.app
        if isinstance(app, DefibApp):
            app.start_flash_doctor(chip=chip, port=port)
