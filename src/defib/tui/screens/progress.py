"""Progress screen: multi-stage progress bars and serial log panel."""

from __future__ import annotations

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
    """Recovery progress screen with stage indicators, progress bar, and log."""

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
                    indicator = StageIndicator(name, id=f"stage-{name.lower().replace('/', '-')}")
                    self._stage_widgets[name.lower()] = indicator
                    yield indicator

        with Vertical(id="progress-container"):
            yield ProgressBar(total=100, show_eta=True, id="main-progress")

        with Vertical(id="log-container"):
            yield RichLog(highlight=True, markup=True, id="log-panel", wrap=True)

        with Horizontal(id="bottom-bar"):
            yield Button("Back", variant="default", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        self._log("Starting recovery session...")
        self.run_worker(self._run_recovery(), exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self.app.pop_screen()

    def _log(self, message: str, style: str = "") -> None:
        log_panel = self.query_one("#log-panel", RichLog)
        timestamp = datetime.now().strftime("%H:%M:%S")
        if style:
            log_panel.write(f"[dim]{timestamp}[/dim] [{style}]{message}[/{style}]")
        else:
            log_panel.write(f"[dim]{timestamp}[/dim] {message}")

    def _map_stage_to_indicator(self, stage: Stage) -> str | None:
        """Map protocol stage to indicator widget name."""
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
        # Update progress bar
        if event.bytes_total > 1:
            progress = self.query_one("#main-progress", ProgressBar)
            progress.update(total=event.bytes_total, progress=event.bytes_sent)

        # Update stage indicator
        indicator_name = self._map_stage_to_indicator(event.stage)
        if indicator_name and indicator_name != self._current_stage:
            # Mark previous stage complete
            if self._current_stage and self._current_stage in self._stage_widgets:
                self._stage_widgets[self._current_stage].set_complete()
            # Mark new stage active
            if indicator_name in self._stage_widgets:
                self._stage_widgets[indicator_name].set_active()
            self._current_stage = indicator_name

        if event.message:
            self._log(event.message)

    def _on_log(self, event: LogEvent) -> None:
        style_map = {"error": "red", "warn": "yellow", "info": "green", "debug": "dim"}
        style = style_map.get(event.level, "")
        self._log(event.message, style=style)

    async def _run_recovery(self) -> None:
        """Execute recovery in a worker thread."""
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
            return
        finally:
            await transport.close()

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
        else:
            self._log(f"Recovery failed: {result.error}", style="red bold")
