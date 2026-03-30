"""Progress screen: recovery progress + post-recovery serial console."""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widgets import (
    Header,
    Footer,
    Static,
    ProgressBar,
    RichLog,
    Button,
    Input,
)

from defib.recovery.events import LogEvent, ProgressEvent, Stage


class StageIndicator(Static):
    """Shows the current stage with status icon."""

    def __init__(self, stage_name: str, id: str | None = None) -> None:  # noqa: A002
        super().__init__(f"○ {stage_name}", id=id)
        self._stage_name = stage_name
        self._status = "pending"

    def set_active(self) -> None:
        self._status = "active"
        self.update(f"◉ {self._stage_name}")
        self.add_class("active")

    def set_complete(self) -> None:
        self._status = "complete"
        self.update(f"✓ {self._stage_name}")
        self.remove_class("active")
        self.add_class("complete")

    def set_failed(self) -> None:
        self._status = "failed"
        self.update(f"✗ {self._stage_name}")
        self.remove_class("active")
        self.add_class("failed")


class ProgressScreen(Screen[None]):
    """Recovery progress screen, then serial console after completion."""

    BINDINGS = [
        ("ctrl+c", "send_break", "Send Ctrl-C to device"),
    ]

    CSS = """
    ProgressScreen {
        layout: vertical;
    }

    #status-container {
        height: auto;
        max-height: 12;
        padding: 1 2;
        border-bottom: thick $accent;
    }

    #info-row {
        height: 3;
        padding: 0 1;
    }

    #stages-row {
        height: auto;
        padding: 0 1;
        layout: horizontal;
    }

    StageIndicator {
        width: auto;
        min-width: 16;
        padding: 0 1;
        color: $text-muted;
    }

    StageIndicator.active {
        color: $accent;
        text-style: bold;
    }

    StageIndicator.complete {
        color: $success;
    }

    StageIndicator.failed {
        color: $error;
    }

    #progress-container {
        height: 3;
        padding: 0 2;
    }

    #log-container {
        height: 1fr;
        border-top: thick $accent;
    }

    #log-panel {
        height: 1fr;
    }

    #console-input {
        dock: bottom;
        margin: 0 2;
    }

    #bottom-bar {
        height: 3;
        padding: 0 2;
        align: right middle;
        dock: bottom;
    }
    """

    def __init__(
        self,
        chip: str,
        firmware_path: str,
        port: str,
        send_break: bool,
    ) -> None:
        super().__init__()
        self._chip = chip
        self._firmware_path = firmware_path
        self._port = port
        self._send_break = send_break
        self._stage_widgets: dict[str, StageIndicator] = {}
        self._current_stage: str | None = None
        self._transport: object | None = None  # Kept open for console
        self._console_mode = False
        self._console_reader_task: asyncio.Task[None] | None = None
        self._log_buffer: list[str] = []  # Plain-text log for export

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="status-container"):
            yield Static(
                f"[bold]Recovering:[/bold] {self._chip} via {self._port}",
                id="info-row",
            )
            with Horizontal(id="stages-row"):
                stages = ["Handshake", "DDR Init", "SPL/GSL", "U-Boot"]
                for name in stages:
                    safe_id = name.lower().replace("/", "-").replace(" ", "-")
                    indicator = StageIndicator(name, id=f"stage-{safe_id}")
                    self._stage_widgets[name.lower()] = indicator
                    yield indicator

        with Vertical(id="progress-container"):
            yield ProgressBar(total=100, show_eta=True, id="main-progress")

        with Vertical(id="log-container"):
            yield RichLog(highlight=True, markup=True, id="log-panel", wrap=True)

        yield Input(
            placeholder="Type command and press Enter (serial console)",
            id="console-input",
        )

        with Horizontal(id="bottom-bar"):
            yield Button("Save Log", variant="success", id="save-log-btn")
            yield Button("Back", variant="default", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        # Hide console input until recovery completes
        self.query_one("#console-input").display = False
        self._log("Starting recovery session...")
        self.run_worker(self._run_recovery(), exclusive=True)

    async def action_send_break(self) -> None:
        """Send Ctrl-C (0x03) to the serial device."""
        if self._console_mode and self._transport is not None:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            try:
                await transport.write(b"\x03")
                self._console_write("^C")
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self._stop_console()
            self.app.pop_screen()
        elif event.button.id == "save-log-btn":
            self._save_log()

    def _save_log(self) -> None:
        """Save log buffer to a timestamped file."""
        from pathlib import Path

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"defib_{self._chip}_{timestamp}.log"
        path = Path.cwd() / filename
        try:
            path.write_text("\n".join(self._log_buffer) + "\n")
            self.notify(f"Log saved: {filename}", severity="information", title="Log Saved")
        except Exception as e:
            self.notify(f"Failed to save: {e}", severity="error", title="Save Error")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send typed command to serial port."""
        if not self._console_mode or self._transport is None:
            return
        cmd = event.value
        event.input.value = ""
        if cmd:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            try:
                await transport.write((cmd + "\n").encode())
            except Exception as e:
                self._console_write(f"[Send error: {e}]\n")

    def _log(self, message: str, style: str = "") -> None:
        log_panel = self.query_one("#log-panel", RichLog)
        timestamp = datetime.now().strftime("%H:%M:%S")
        if style:
            log_panel.write(f"[dim]{timestamp}[/dim] [{style}]{message}[/{style}]")
        else:
            log_panel.write(f"[dim]{timestamp}[/dim] {message}")
        self._log_buffer.append(f"{timestamp} {message}")

    def _console_write(self, text: str) -> None:
        """Write raw text to the log panel (no timestamp, for serial output)."""
        log_panel = self.query_one("#log-panel", RichLog)
        log_panel.write(text)
        self._log_buffer.append(text)

    def _map_stage_to_indicator(self, stage: Stage) -> str | None:
        mapping: dict[Stage, str] = {
            Stage.HANDSHAKE: "handshake",
            Stage.DDR_INIT: "ddr init",
            Stage.SPL: "spl/gsl",
            Stage.GSL: "spl/gsl",
            Stage.DDR_TABLE: "ddr init",
            Stage.DDR_TRAINING: "ddr init",
            Stage.HEAD_AREA: "spl/gsl",
            Stage.AUX_AREA: "spl/gsl",
            Stage.BOOT_IMAGE: "u-boot",
            Stage.UBOOT: "u-boot",
            Stage.BOARD_ID: "ddr init",
        }
        return mapping.get(stage)

    def _on_progress(self, event: ProgressEvent) -> None:
        if event.bytes_total > 1:
            progress = self.query_one("#main-progress", ProgressBar)
            progress.update(total=event.bytes_total, progress=event.bytes_sent)

        indicator_name = self._map_stage_to_indicator(event.stage)
        if indicator_name and indicator_name != self._current_stage:
            if self._current_stage and self._current_stage in self._stage_widgets:
                self._stage_widgets[self._current_stage].set_complete()
            if indicator_name in self._stage_widgets:
                self._stage_widgets[indicator_name].set_active()
            self._current_stage = indicator_name

        if event.message and event.stage != Stage.COMPLETE:
            self._log(event.message)

    def _on_log(self, event: LogEvent) -> None:
        style_map = {"error": "red", "warn": "yellow", "info": "green", "debug": "dim"}
        style = style_map.get(event.level, "")
        self._log(event.message, style=style)

    def _enter_console_mode(self) -> None:
        """Switch UI to serial console mode."""
        self._console_mode = True
        # Hide progress bar, show console input
        self.query_one("#progress-container").display = False
        self.query_one("#status-container").display = False
        console_input = self.query_one("#console-input", Input)
        console_input.display = True
        console_input.focus()

        info = self.query_one("#info-row", Static)
        info.update(f"[bold]Serial Console:[/bold] {self._port}")

        self._log("--- Serial Console (type commands below, press Enter to send) ---",
                  style="cyan bold")

        # Start background reader
        self._console_reader_task = asyncio.ensure_future(self._console_read_loop())

    async def _console_read_loop(self) -> None:
        """Background task: read serial data, display it, auto-interrupt autoboot."""
        from defib.transport.base import Transport, TransportTimeout
        transport: Transport = self._transport  # type: ignore[assignment]
        buf = bytearray()
        autoboot_handled = False
        # Rolling window of recent output for autoboot detection
        recent = ""

        while self._console_mode and transport is not None:
            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    data = await transport.read(min(waiting, 1024), timeout=0.1)
                    buf.extend(data)
                    text = buf.decode("ascii", errors="replace")
                    if "\n" in text or len(buf) > 256:
                        self._console_write(text)
                        # Track recent output for autoboot detection
                        recent += text
                        if len(recent) > 2048:
                            recent = recent[-1024:]
                        buf.clear()

                    # Auto-detect autoboot and send Ctrl-C
                    if not autoboot_handled and "autoboot" in recent.lower():
                        autoboot_handled = True
                        self._log("Autoboot detected! Sending Ctrl-C...", style="yellow bold")
                        for _ in range(20):
                            await transport.write(b"\x03")
                            await asyncio.sleep(0.1)
                else:
                    if buf:
                        text = buf.decode("ascii", errors="replace")
                        self._console_write(text)
                        recent += text
                        if len(recent) > 2048:
                            recent = recent[-1024:]
                        buf.clear()
                    await asyncio.sleep(0.05)
            except TransportTimeout:
                await asyncio.sleep(0.05)
            except Exception:
                break

    def _stop_console(self) -> None:
        """Stop console mode and close transport."""
        self._console_mode = False
        if self._console_reader_task and not self._console_reader_task.done():
            self._console_reader_task.cancel()
        if self._transport is not None:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            asyncio.ensure_future(transport.close())
            self._transport = None

    async def _run_recovery(self) -> None:
        """Execute recovery, then enter console mode on success."""
        from defib.recovery.session import RecoverySession
        from defib.transport.serial_platform import create_transport, normalize_port_name

        try:
            session = RecoverySession(chip=self._chip, firmware_path=self._firmware_path)
        except (ValueError, FileNotFoundError) as e:
            self._log(f"Error: {e}", style="red bold")
            return

        self._log(f"Protocol: {session.protocol_name}")

        try:
            transport = await create_transport(normalize_port_name(self._port))
        except Exception as e:
            self._log(f"Failed to open serial port: {e}", style="red bold")
            return

        try:
            result = await session.run(
                transport,
                on_progress=self._on_progress,
                on_log=self._on_log,
                send_break=self._send_break,
            )
        except Exception as e:
            self._log(f"Recovery error: {e}", style="red bold")
            await transport.close()
            return

        # Mark final stage
        if self._current_stage and self._current_stage in self._stage_widgets:
            if result.success:
                self._stage_widgets[self._current_stage].set_complete()
            else:
                self._stage_widgets[self._current_stage].set_failed()

        if result.success:
            self._log(f"Recovery complete! ({result.elapsed_ms:.0f}ms)", style="green bold")
            progress = self.query_one("#main-progress", ProgressBar)
            progress.update(total=100, progress=100)
            # Keep transport open and enter console mode
            self._transport = transport
            self._enter_console_mode()
        else:
            self._log(f"Recovery failed: {result.error}", style="red bold")
            await transport.close()
