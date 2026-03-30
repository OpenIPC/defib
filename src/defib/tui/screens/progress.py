"""Progress screen: recovery progress + post-recovery serial terminal."""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.events import Key
from textual.screen import Screen
from textual.widgets import (
    Header,
    Footer,
    Static,
    ProgressBar,
    RichLog,
    TextArea,
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
    """Recovery progress, then direct serial terminal after completion."""

    BINDINGS = [
        ("ctrl+c", "send_break", "Ctrl-C → device"),
        ("ctrl+s", "save_log", "Save log"),
        ("ctrl+d", "dump_flash", "Dump flash"),
    ]

    CSS = """
    ProgressScreen { layout: vertical; }

    #status-container {
        height: auto; max-height: 12;
        padding: 1 2; border-bottom: thick $accent;
    }
    #info-row { height: 3; padding: 0 1; }
    #stages-row { height: auto; padding: 0 1; layout: horizontal; }

    StageIndicator { width: auto; min-width: 16; padding: 0 1; color: $text-muted; }
    StageIndicator.active { color: $accent; text-style: bold; }
    StageIndicator.complete { color: $success; }
    StageIndicator.failed { color: $error; }

    #progress-container { height: 3; padding: 0 2; }
    #log-container { height: 1fr; }
    #log-panel { height: 1fr; }

    #terminal {
        height: 1fr;
        border: none;
    }

    #bottom-bar {
        height: 3; padding: 0 2;
        layout: horizontal; align: right middle;
        dock: bottom;
    }
    """

    def __init__(
        self, chip: str, firmware_path: str, port: str, send_break: bool,
    ) -> None:
        super().__init__()
        self._chip = chip
        self._firmware_path = firmware_path
        self._port = port
        self._send_break = send_break
        self._stage_widgets: dict[str, StageIndicator] = {}
        self._current_stage: str | None = None
        self._transport: object | None = None
        self._console_mode = False
        self._console_reader_task: asyncio.Task[None] | None = None
        self._log_buffer: list[str] = []
        self._term_content = ""  # Raw terminal text content

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="status-container"):
            yield Static(
                f"[bold]Recovering:[/bold] {self._chip} via {self._port}",
                id="info-row",
            )
            with Horizontal(id="stages-row"):
                for name in ["Handshake", "DDR Init", "SPL/GSL", "U-Boot"]:
                    safe_id = name.lower().replace("/", "-").replace(" ", "-")
                    indicator = StageIndicator(name, id=f"stage-{safe_id}")
                    self._stage_widgets[name.lower()] = indicator
                    yield indicator

        with Vertical(id="progress-container"):
            yield ProgressBar(total=100, show_eta=True, id="main-progress")

        with Vertical(id="log-container"):
            yield RichLog(highlight=True, markup=True, id="log-panel", wrap=True)

        yield TextArea("", id="terminal", read_only=True, show_line_numbers=False)

        with Horizontal(id="bottom-bar"):
            yield Button("Back", variant="default", id="back-btn")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#terminal", TextArea).display = False
        self._log("Starting recovery session...")
        self.run_worker(self._run_recovery(), exclusive=True)

    # --- Key handling: direct terminal mode ---

    async def on_key(self, event: Key) -> None:
        """In console mode, send keystrokes directly to serial."""
        if not self._console_mode or self._transport is None:
            return

        from defib.transport.base import Transport
        transport: Transport = self._transport  # type: ignore[assignment]

        # Let Ctrl-key combos pass through to Textual bindings
        # (Ctrl-C, Ctrl-Q, Ctrl-S, Ctrl-D are handled as actions)
        if event.key.startswith("ctrl+"):
            return

        event.prevent_default()
        event.stop()
        try:
            key = event.key
            if key == "enter":
                await transport.write(b"\r")
            elif key == "backspace":
                await transport.write(b"\x08")  # BS
            elif key == "tab":
                await transport.write(b"\t")
            elif key == "up":
                await transport.write(b"\x1b[A")
            elif key == "down":
                await transport.write(b"\x1b[B")
            elif key == "escape":
                await transport.write(b"\x1b")
            elif event.character:
                await transport.write(event.character.encode("utf-8"))
        except Exception:
            pass

    async def action_send_break(self) -> None:
        """Send Ctrl-C (0x03) to the serial device."""
        if self._console_mode and self._transport is not None:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            try:
                await transport.write(b"\x03")
            except Exception:
                pass

    def action_save_log(self) -> None:
        self._save_log()

    def action_dump_flash(self) -> None:
        self._start_flash_dump()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back-btn":
            self._stop_console()
            self.app.pop_screen()

    # --- Logging ---

    def _log(self, message: str, style: str = "") -> None:
        log_panel = self.query_one("#log-panel", RichLog)
        timestamp = datetime.now().strftime("%H:%M:%S")
        if style:
            log_panel.write(f"[dim]{timestamp}[/dim] [{style}]{message}[/{style}]")
        else:
            log_panel.write(f"[dim]{timestamp}[/dim] {message}")
        self._log_buffer.append(f"{timestamp} {message}")

    def _console_write(self, text: str) -> None:
        """Append serial output to the terminal TextArea.

        Handles control characters:
        - \\x08 (BS): delete previous character (U-Boot sends BS+space+BS to erase)
        - \\r\\n / \\r: newline
        """
        if not text:
            return
        self._log_buffer.append(text)
        try:
            term = self.query_one("#terminal", TextArea)
            self._term_content += text
            # Interpret backspaces in the buffer
            result: list[str] = []
            for ch in self._term_content:
                if ch == "\x08":
                    # Only delete within current line, never past \n
                    if result and result[-1] != "\n":
                        result.pop()
                elif ch == "\r":
                    pass  # Skip bare CR, \n handles line breaks
                else:
                    result.append(ch)
            new_content = "".join(result)
            if new_content != term.text:
                term.clear()
                term.insert(new_content, location=(0, 0))
                term.scroll_end(animate=False)
        except Exception:
            pass

    def _save_log(self) -> None:
        from pathlib import Path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"defib_{self._chip}_{timestamp}.log"
        path = Path.cwd() / filename
        try:
            path.write_text("\n".join(self._log_buffer) + "\n")
            self.notify(f"Log saved: {filename}", severity="information", title="Log Saved")
        except Exception as e:
            self.notify(f"Failed to save: {e}", severity="error", title="Save Error")

    # --- Stage tracking ---

    def _map_stage_to_indicator(self, stage: Stage) -> str | None:
        mapping: dict[Stage, str] = {
            Stage.HANDSHAKE: "handshake", Stage.DDR_INIT: "ddr init",
            Stage.SPL: "spl/gsl", Stage.GSL: "spl/gsl",
            Stage.DDR_TABLE: "ddr init", Stage.DDR_TRAINING: "ddr init",
            Stage.HEAD_AREA: "spl/gsl", Stage.AUX_AREA: "spl/gsl",
            Stage.BOOT_IMAGE: "u-boot", Stage.UBOOT: "u-boot",
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
        self._log(event.message, style=style_map.get(event.level, ""))

    # --- Console mode ---

    def _enter_console_mode(self) -> None:
        self._console_mode = True
        # Switch from recovery view to terminal view
        self.query_one("#progress-container").display = False
        self.query_one("#status-container").display = False
        self.query_one("#log-container").display = False
        self.query_one("#terminal", TextArea).display = True
        # Start reader
        self._console_reader_task = asyncio.ensure_future(self._console_read_loop())

    async def _console_read_loop(self) -> None:
        from defib.transport.base import Transport, TransportTimeout
        transport: Transport = self._transport  # type: ignore[assignment]
        autoboot_handled = False
        recent = ""

        while self._console_mode and transport is not None:
            try:
                waiting = await transport.bytes_waiting()
                if waiting > 0:
                    # Brief pause to let more bytes arrive (batch reads)
                    await asyncio.sleep(0.02)
                    waiting = await transport.bytes_waiting()
                    data = await transport.read(max(waiting, 1), timeout=0.1)
                    text = data.decode("ascii", errors="replace")
                    self._console_write(text)
                    recent += text
                    if len(recent) > 2048:
                        recent = recent[-1024:]
                    # Auto-detect autoboot
                    if not autoboot_handled and "autoboot" in recent.lower():
                        autoboot_handled = True
                        self._log("Autoboot detected! Sending Ctrl-C...", style="yellow bold")
                        for _ in range(20):
                            await transport.write(b"\x03")
                            # Read and display response between Ctrl-C sends
                            await asyncio.sleep(0.1)
                            try:
                                w = await transport.bytes_waiting()
                                if w > 0:
                                    resp = await transport.read(w, timeout=0.05)
                                    self._console_write(resp.decode("ascii", errors="replace"))
                            except Exception:
                                pass
                else:
                    await asyncio.sleep(0.05)
            except TransportTimeout:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"Console error: {e}", style="red")
                break

    def _stop_console(self) -> None:
        self._console_mode = False
        if self._console_reader_task and not self._console_reader_task.done():
            self._console_reader_task.cancel()
        if self._transport is not None:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            asyncio.ensure_future(transport.close())
            self._transport = None

    # --- Flash dump ---

    def _start_flash_dump(self) -> None:
        if not self._console_mode or self._transport is None:
            self.notify("Connect to device first", severity="error")
            return
        was_console = self._console_mode
        self._console_mode = False
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"flash_{self._chip}_{timestamp}.bin"
        self._log("Starting flash dump...", style="cyan bold")
        self.run_worker(self._run_flash_dump(filename, was_console), exclusive=False)

    async def _run_flash_dump(self, filename: str, resume_console: bool) -> None:
        from pathlib import Path
        from defib.flashdump import dump_flash
        from defib.transport.base import Transport

        transport: Transport = self._transport  # type: ignore[assignment]
        output_path = str(Path.cwd() / filename)

        try:
            boot_log = "\n".join(self._log_buffer)
            bytes_dumped = await dump_flash(
                transport, output_path, boot_log=boot_log,
                on_progress=lambda done, total: self._log(
                    f"  Dump: {done*100//total}% ({done//(1024*1024)}/{total//(1024*1024)} MB)"
                ) if done % (256 * 1024) < 65536 else None,
                on_log=lambda msg: self._log(msg),
            )
            self._log(f"Flash dump saved: {filename} ({bytes_dumped} bytes)", style="green bold")
            self.notify(f"Dump saved: {filename}", severity="information", title="Flash Dump")
        except asyncio.CancelledError:
            return
        except Exception as e:
            self._log(f"Flash dump failed: {e}", style="red bold")
            self.notify(str(e), severity="error", title="Dump Failed")
        finally:
            if resume_console:
                self._console_mode = True
                self._console_reader_task = asyncio.ensure_future(self._console_read_loop())

    # --- Recovery ---

    async def _run_recovery(self) -> None:
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

        if self._current_stage and self._current_stage in self._stage_widgets:
            if result.success:
                self._stage_widgets[self._current_stage].set_complete()
            else:
                self._stage_widgets[self._current_stage].set_failed()

        if result.success:
            self._log(f"Recovery complete! ({result.elapsed_ms:.0f}ms)", style="green bold")
            progress = self.query_one("#main-progress", ProgressBar)
            progress.update(total=100, progress=100)
            self._transport = transport
            self._enter_console_mode()
        else:
            self._log(f"Recovery failed: {result.error}", style="red bold")
            await transport.close()
