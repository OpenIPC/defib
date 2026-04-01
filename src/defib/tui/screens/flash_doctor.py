"""Flash Doctor screen — Norton Disk Doctor-style SPI NOR flash health scanner.

Displays a retro-styled sector grid with animated scanning, color-coded
health indicators, and a comprehensive summary panel.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (
    Header,
    Footer,
    Static,
    Button,
    RichLog,
)
from textual.reactive import reactive
from textual.message import Message


# ── Block characters for the sector grid ─────────────────────────────────

# Norton Disk Doctor used solid colored blocks. We use Unicode block
# elements for that authentic retro feel.
BLOCK_PENDING = "░"  # not yet scanned
BLOCK_SCANNING = "▒"  # currently being scanned
BLOCK_GOOD = "█"  # healthy sector
BLOCK_EMPTY = "·"  # erased (all 0xFF)
BLOCK_DEAD = "▓"  # stuck/dead
BLOCK_UNSTABLE = "▒"  # data degrading
BLOCK_ERROR = "✕"  # read error


BOX_INNER = 42  # visible cells between ║ and ║


def _cell_len(text: str) -> int:
    """Get display width of text in terminal cells."""
    try:
        from rich.cells import cell_len
        return cell_len(text)
    except ImportError:
        return len(text)


def _center_in_box(text: str, markup_text: str) -> str:
    """Pad plain `text` width to BOX_INNER, wrapping `markup_text` centered."""
    w = _cell_len(text)
    pad = BOX_INNER - w
    left = pad // 2
    right = pad - left
    c = "bold bright_cyan"
    return f"[{c}]║[/]{' ' * left}{markup_text}{' ' * right}[{c}]║[/]"


def _build_banner(subtitle: str) -> str:
    """Build the fixed-width ASCII banner with centered subtitle."""
    title_plain = "F L A S H   D O C T O R"
    title_markup = f"[bold bright_white]{title_plain}[/]"

    c = "bold bright_cyan"
    return (
        f"[{c}]╔{'═' * BOX_INNER}╗[/]\n"
        f"{_center_in_box(title_plain, title_markup)}\n"
        f"{_center_in_box(subtitle, f'[dim]{subtitle}[/]')}\n"
        f"[{c}]╚{'═' * BOX_INNER}╝[/]"
    )



class SectorGrid(Widget):
    """Custom widget: Norton Disk Doctor-style sector block grid.

    Renders a grid of colored blocks representing flash sectors.
    Each block shows sector health with retro color coding.
    """

    DEFAULT_CSS = """
    SectorGrid {
        height: auto;
        padding: 0 1;
    }
    """

    # Reactive property triggers re-render
    grid_version = reactive(0)

    def __init__(
        self,
        num_sectors: int = 0,
        cols: int = 32,
        *,
        id: str | None = None,  # noqa: A002
    ) -> None:
        super().__init__(id=id)
        self._num_sectors = num_sectors
        self._cols = cols
        # Status per sector: None=pending, or SectorStatus value
        self._statuses: list[int | None] = [None] * num_sectors
        self._scanning_idx: int | None = None
        self._sector_size = 0x10000

    def set_geometry(self, num_sectors: int, sector_size: int) -> None:
        self._num_sectors = num_sectors
        self._sector_size = sector_size
        self._statuses = [None] * num_sectors
        self._scanning_idx = None
        rows = (num_sectors + self._cols - 1) // self._cols if num_sectors else 1
        self.styles.height = rows
        self.grid_version += 1

    def set_sector(self, index: int, status: int) -> None:
        if 0 <= index < len(self._statuses):
            self._statuses[index] = status
            self._scanning_idx = index + 1 if index + 1 < self._num_sectors else None
            self.grid_version += 1

    def render(self) -> str:
        from defib.agent.client import SectorStatus

        if self._num_sectors == 0:
            return "  No sectors to display"

        status_map = {
            SectorStatus.GOOD: (BLOCK_GOOD, "green"),
            SectorStatus.EMPTY: (BLOCK_EMPTY, "bright_black"),
            SectorStatus.STUCK_ZERO: (BLOCK_DEAD, "red"),
            SectorStatus.STUCK_PATTERN: (BLOCK_DEAD, "red"),
            SectorStatus.UNSTABLE: (BLOCK_UNSTABLE, "yellow"),
            SectorStatus.READ_ERROR: (BLOCK_ERROR, "bright_red"),
        }

        lines: list[str] = []
        rows = (self._num_sectors + self._cols - 1) // self._cols
        for row in range(rows):
            addr = row * self._cols * self._sector_size
            parts: list[str] = [f"  [dim]0x{addr:06X}[/dim] │ "]
            for col in range(self._cols):
                idx = row * self._cols + col
                if idx >= self._num_sectors:
                    parts.append(" ")
                    continue

                status = self._statuses[idx]
                if status is None:
                    if idx == self._scanning_idx:
                        parts.append(f"[bold bright_cyan on dark_cyan]{BLOCK_SCANNING}[/]")
                    else:
                        parts.append(f"[bright_black]{BLOCK_PENDING}[/]")
                else:
                    char, color = status_map.get(SectorStatus(status), (BLOCK_ERROR, "red"))
                    if status == SectorStatus.GOOD:
                        parts.append(f"[{color}]{char}[/]")
                    elif status == SectorStatus.EMPTY:
                        parts.append(f"[{color}]{char}[/]")
                    else:
                        parts.append(f"[bold {color}]{char}[/]")
            parts.append(" │")
            lines.append("".join(parts))

        return "\n".join(lines)


class ScanStats(Static):
    """Live-updating scan statistics panel."""

    DEFAULT_CSS = """
    ScanStats {
        height: auto;
        padding: 0 2;
    }
    """

    def update_stats(
        self,
        scanned: int,
        total: int,
        good: int,
        empty: int,
        bad: int,
        unstable: int,
        elapsed: float,
    ) -> None:
        if total == 0:
            self.update("")
            return

        pct = scanned * 100 // total
        rate = scanned / elapsed if elapsed > 0 else 0
        eta = (total - scanned) / rate if rate > 0 and scanned < total else 0

        elapsed_m, elapsed_s = divmod(int(elapsed), 60)
        eta_m, eta_s = divmod(int(eta), 60)

        bar_width = 30
        filled = int(bar_width * scanned / total)
        bar = "━" * filled + "╺" + "─" * (bar_width - filled - 1) if scanned < total else "━" * bar_width

        lines = []
        if scanned < total:
            lines.append(
                f"  [bold bright_cyan]Scanning[/] sector [bold]{scanned}[/]/{total}  "
                f"[dim]{elapsed_m}:{elapsed_s:02d} elapsed  ETA {eta_m}:{eta_s:02d}[/]"
            )
            lines.append(f"  [bright_cyan]{bar}[/] {pct}%")
        else:
            lines.append(
                f"  [bold green]Scan complete[/]  {total} sectors in "
                f"{elapsed_m}:{elapsed_s:02d}"
            )
            lines.append(f"  [green]{bar}[/] 100%")

        lines.append("")
        lines.append(
            f"  [green]█[/] Good: [bold]{good:>4}[/]  "
            f"[bright_black]·[/] Empty: [bold]{empty:>4}[/]  "
            f"[yellow]▒[/] Unstable: [bold]{unstable:>3}[/]  "
            f"[red]▓[/] Dead: [bold]{bad:>3}[/]"
        )

        self.update("\n".join(lines))


class FlashDoctorScreen(Screen[None]):
    """Flash Doctor — Norton Disk Doctor-style SPI NOR health scanner."""

    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("r", "start_scan", "Scan"),
        ("s", "save_dump", "Save data"),
    ]

    CSS = """
    FlashDoctorScreen {
        layout: vertical;
    }

    #doctor-container {
        width: 100%;
        height: 100%;
        layout: vertical;
    }

    /* ── Title banner ── */
    #doctor-banner {
        width: 100%;
        height: auto;
        content-align: center middle;
        text-align: center;
        padding: 1 0 0 0;
        color: $accent;
    }

    /* ── Setup panel (pre-scan) ── */
    #setup-panel {
        width: 100%;
        height: auto;
        content-align: center middle;
        text-align: center;
        padding: 1 0;
    }

    /* ── Scan view ── */
    #scan-view {
        width: 100%;
        height: 1fr;
        layout: vertical;
    }

    #grid-frame {
        width: 100%;
        height: auto;
        min-height: 16;
        border: thick $accent;
        margin: 0 2;
        padding: 1 0;
        background: $panel;
    }

    #grid-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        width: 100%;
        padding: 0 0 1 0;
    }

    #grid-legend {
        padding: 1 2 0 2;
        height: auto;
        color: $text;
    }

    #stats-panel {
        height: auto;
        max-height: 10;
        margin: 0 2;
        padding: 1 0;
        border: tall $accent;
        background: $panel;
    }

    #stats-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        width: 100%;
        margin-bottom: 1;
    }

    /* ── Results log ── */
    #results-log {
        height: 8;
        margin: 0 2;
        border: tall $accent;
    }

    /* ── Bottom buttons ── */
    #doctor-buttons {
        dock: bottom;
        height: 3;
        padding: 0 2;
        layout: horizontal;
        align: right middle;
    }

    #scan-btn {
        min-width: 16;
        margin-right: 1;
    }

    #save-btn {
        min-width: 16;
        margin-right: 1;
    }

    #back-btn-doctor {
        min-width: 12;
    }
    """

    class ScanComplete(Message):
        """Emitted when a scan finishes."""

    def __init__(self, port: str = "", chip: str = "") -> None:
        super().__init__()
        self._port = port
        self._chip = chip
        self._transport: object | None = None
        self._client: object | None = None
        self._scanning = False
        self._scan_result: object | None = None
        self._t0 = 0.0
        self._scanned = 0
        self._good = 0
        self._empty = 0
        self._bad = 0
        self._unstable = 0
        self._num_sectors = 0
        self._sector_size = 0x10000
        self._flash_size = 0

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="doctor-container"):
            yield Static(
                _build_banner("SPI NOR Health Scanner — sector by sector"),
                id="doctor-banner",
            )

            chip_info = f"  [bold]Chip:[/] {self._chip}  " if self._chip else "  "
            yield Static(
                f"{chip_info}[bold]Port:[/] {self._port}\n"
                "  [dim]Connects to running agent, or uploads if needed[/]",
                id="setup-panel",
            )
            yield Button(
                "Start",
                variant="success",
                id="connect-scan-btn",
            )

            with Vertical(id="scan-view"):
                with Vertical(id="grid-frame"):
                    yield Static("", id="grid-title")
                    yield SectorGrid(id="sector-grid")
                    yield Static(
                        "  [green]█[/] Good  "
                        "[bright_black]·[/] Empty  "
                        "[red]▓[/] Dead/Stuck  "
                        "[yellow]▒[/] Unstable  "
                        "[bright_red]✕[/] Error  "
                        "[bright_black]░[/] Pending  "
                        "[bold bright_cyan on dark_cyan]▒[/] Scanning",
                        id="grid-legend",
                    )

                with Vertical(id="stats-panel"):
                    yield Static("Diagnostics", id="stats-title")
                    yield ScanStats(id="scan-stats")

                yield RichLog(
                    highlight=True, markup=True, id="results-log", wrap=True,
                )

            with Horizontal(id="doctor-buttons"):
                yield Button("Re-scan", variant="success", id="scan-btn", disabled=True)
                yield Button("Save Data", variant="warning", id="save-btn", disabled=True)
                yield Button("Back", variant="default", id="back-btn-doctor")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scan-view").display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect-scan-btn":
            self._upload_and_scan()
        elif event.button.id == "scan-btn":
            self._start_rescan()
        elif event.button.id == "save-btn":
            import threading
            threading.Thread(
                target=lambda: asyncio.run(self._save_recoverable_data()),
                daemon=True,
            ).start()
        elif event.button.id == "back-btn-doctor":
            self.action_go_back()

    def action_go_back(self) -> None:
        self._disconnect()
        self.app.pop_screen()

    def action_start_scan(self) -> None:
        if not self._scanning and self._client is not None:
            self._start_rescan()

    def action_save_dump(self) -> None:
        if self._scan_result is not None:
            self.run_worker(self._save_recoverable_data(), exclusive=True)

    # ── Connection ───────────────────────────────────────────────────────

    def _upload_and_scan(self) -> None:
        """Upload agent, connect, and scan — all in one flow."""
        btn = self.query_one("#connect-scan-btn", Button)
        btn.disabled = True
        btn.label = "Waiting for boot..."

        self.query_one("#setup-panel").display = False
        self.query_one("#connect-scan-btn").display = False
        self.query_one("#doctor-banner").display = False
        self.query_one("#scan-view").display = True
        self._log("Power-cycle the camera now!")

        self.run_worker(self._do_upload_and_connect(), exclusive=True)

    async def _do_upload_and_connect(self) -> None:
        """Try connecting to running agent first, upload if needed."""
        import asyncio as aio

        from defib.agent.client import FlashAgentClient, get_agent_binary
        from defib.transport.serial import SerialTransport

        chip = self._chip
        port = self._port

        # Try connecting to a running agent first
        try:
            transport = await SerialTransport.create(port)
        except Exception as e:
            self._log(f"[red]Port error:[/] {e}")
            return

        self._log("Checking for running agent...")
        client = FlashAgentClient(transport, chip)
        if await client.connect(timeout=3.0):
            info = await client.get_info()
            if info:
                self._log("[green]Agent already running![/]")
                await self._connected(transport, client, info)
                return

        # No agent running — upload
        await transport.close()
        if not chip:
            self._log("[red]No agent running and no chip selected — "
                       "go back and select a chip to upload[/]")
            return

        self._log("No agent found. Uploading...")

        agent_path = get_agent_binary(chip)
        if not agent_path:
            self._log(f"[red]No agent binary for '{chip}'[/]")
            return
        agent_data = agent_path.read_bytes()

        from defib.firmware import get_cached_path, download_firmware, has_firmware
        from defib.profiles.loader import load_profile
        from defib.protocol.hisilicon_standard import HiSiliconStandard
        from defib.recovery.events import ProgressEvent

        try:
            profile = load_profile(chip)
        except Exception as e:
            self._log(f"[red]Unknown chip '{chip}':[/] {e}")
            return

        cached_fw = get_cached_path(chip)
        if not cached_fw:
            if has_firmware(chip):
                self._log("Downloading U-Boot for SPL...")
                try:
                    cached_fw = download_firmware(chip)
                except Exception as e:
                    self._log(f"[red]Download failed:[/] {e}")
                    return
            else:
                self._log(f"[red]No firmware available for '{chip}'[/]")
                return

        spl_data = cached_fw.read_bytes()[:profile.spl_max_size]
        self._log(
            f"Agent: [cyan]{agent_path.name}[/] ({len(agent_data)} bytes)  "
            f"SPL: {len(spl_data)} bytes"
        )

        transport = await SerialTransport.create(port)

        protocol = HiSiliconStandard()
        protocol.set_profile(profile)

        def on_progress(e: ProgressEvent) -> None:
            if e.message:
                self._log(f"  {e.message}")

        self._log("[yellow]Waiting for bootrom — power-cycle now![/]")
        hs = await protocol.handshake(transport, on_progress)
        if not hs.success:
            self._log("[red]Handshake failed[/]")
            await transport.close()
            return

        result = await protocol.send_firmware(
            transport, agent_data, on_progress, spl_override=spl_data,
        )
        if not result.success:
            self._log(f"[red]Upload failed:[/] {result.error}")
            await transport.close()
            return

        self._log("[green]Agent uploaded![/] Waiting for READY...")

        await transport.close()
        await aio.sleep(2)
        transport = await SerialTransport.create(port)

        client = FlashAgentClient(transport, chip)
        if not await client.connect(timeout=10.0):
            self._log("[red]Agent not responding after upload[/]")
            await transport.close()
            return

        info = await client.get_info()
        await self._connected(transport, client, info)

    async def _connected(
        self, transport: object, client: object, info: dict,  # type: ignore[type-arg]
    ) -> None:
        """Set up state after successful agent connection and start scan."""
        from defib.agent.client import FlashAgentClient

        c: FlashAgentClient = client  # type: ignore[assignment]
        self._transport = transport
        self._client = c
        self._flash_size = int(info.get("flash_size", 0))
        self._sector_size = int(info.get("sector_size", 0x10000))
        self._num_sectors = self._flash_size // self._sector_size if self._sector_size else 0
        jedec = str(info.get("jedec_id", "??????"))

        self._log(
            f"[green]Connected![/]  JEDEC: [bold]{jedec}[/]  "
            f"Flash: [bold]{self._flash_size // 1024}KB[/]  "
            f"Sectors: [bold]{self._num_sectors}[/] × {self._sector_size // 1024}KB"
        )

        subtitle = f"{jedec} — {self._flash_size // 1024}KB — {self._num_sectors} sectors"
        self.query_one("#doctor-banner", Static).update(_build_banner(subtitle))

        grid = self.query_one("#sector-grid", SectorGrid)
        grid.set_geometry(self._num_sectors, self._sector_size)
        self.query_one("#grid-title", Static).update(
            f"[bold]Flash Map[/] — {self._num_sectors} sectors"
        )

        self._launch_scan()

    def _start_rescan(self) -> None:
        if self._scanning:
            return
        self._launch_scan()

    def _launch_scan(self) -> None:
        """Prepare UI and start scan in a plain background thread."""
        import threading

        from defib.agent.client import SectorResult

        self._scanning = True
        self._pending_sectors: list[SectorResult] = []
        self._scanned = 0
        self._good = 0
        self._empty = 0
        self._bad = 0
        self._unstable = 0
        self._t0 = time.time()

        self.query_one("#scan-btn", Button).disabled = True
        self.query_one("#save-btn", Button).disabled = True

        grid = self.query_one("#sector-grid", SectorGrid)
        grid.set_geometry(self._num_sectors, self._sector_size)

        stats = self.query_one("#scan-stats", ScanStats)
        stats.update_stats(0, self._num_sectors, 0, 0, 0, 0, 0)

        self._log("[bright_cyan]Scanning flash...[/]")

        # Timer polls for results from the scan thread at ~12fps
        self._scan_timer = self.set_interval(1 / 12, self._poll_scan_results)

        # Plain thread — no Textual worker, no event loop interference
        thread = threading.Thread(target=self._scan_thread_fn, daemon=True)
        thread.start()

    def _scan_thread_fn(self) -> None:
        """Run scan_flash in a plain thread (blocking serial I/O happens here)."""
        import asyncio as _aio

        from defib.agent.client import FlashAgentClient, FALLBACK_BAUD, SectorResult

        client: FlashAgentClient = self._client  # type: ignore[assignment]

        # Reset baud to 115200 — the agent may have reverted after idle timeout
        port = getattr(client._transport, '_port', None)
        if port is not None:
            port.baudrate = FALLBACK_BAUD
        client._current_baud = FALLBACK_BAUD

        def on_sector(result: SectorResult) -> None:
            self._pending_sectors.append(result)

        try:
            scan_result = _aio.run(client.scan_flash(on_sector=on_sector))
            self._scan_result = scan_result
        except Exception as e:
            self.app.call_from_thread(self._scan_error, str(e))
            return

        self.app.call_from_thread(self._scan_finished, scan_result)

    def _poll_scan_results(self) -> None:
        """Timer callback: drain pending sectors and update grid (main thread)."""
        from defib.agent.client import SectorStatus

        if not self._pending_sectors:
            return

        batch = list(self._pending_sectors)
        self._pending_sectors.clear()

        grid = self.query_one("#sector-grid", SectorGrid)
        for result in batch:
            grid.set_sector(result.index, result.status)
            self._scanned = result.index + 1
            if result.status == SectorStatus.GOOD:
                self._good += 1
            elif result.status == SectorStatus.EMPTY:
                self._empty += 1
            elif result.status == SectorStatus.UNSTABLE:
                self._unstable += 1
            else:
                self._bad += 1

        self.query_one("#scan-stats", ScanStats).update_stats(
            self._scanned, self._num_sectors,
            self._good, self._empty, self._bad, self._unstable,
            time.time() - self._t0,
        )

    def _scan_error(self, error: str) -> None:
        """Handle scan error (called on main thread via call_from_thread)."""
        self._scanning = False
        if hasattr(self, "_scan_timer"):
            self._scan_timer.stop()
        self._log(f"[red bold]Scan error:[/] {error}")
        self.query_one("#scan-btn", Button).disabled = False

    def _scan_finished(self, scan_result: object) -> None:
        """Show final results (called on main thread via call_from_thread)."""
        from defib.agent.client import ScanResult

        self._scanning = False
        if hasattr(self, "_scan_timer"):
            self._scan_timer.stop()

        # Flush remaining sectors
        self._poll_scan_results()

        result: ScanResult = scan_result  # type: ignore[assignment]
        elapsed = time.time() - self._t0

        self.query_one("#scan-stats", ScanStats).update_stats(
            self._num_sectors, self._num_sectors,
            self._good, self._empty, self._bad, self._unstable,
            elapsed,
        )

        self._log("")
        if self._bad == 0 and self._unstable == 0:
            self._log("[green bold]━━━ Flash is healthy! ━━━[/]")
        else:
            if self._bad > 0:
                self._log(f"[red bold]━━━ {self._bad} BAD SECTOR{'S' if self._bad != 1 else ''} FOUND ━━━[/]")
                for s in result.bad:
                    self._log(f"  [red]▓[/] Sector {s.index:>3d}  0x{s.address:06X}  [red]{s.status.name}[/]")
            if self._unstable > 0:
                self._log(f"[yellow bold]━━━ {self._unstable} UNSTABLE SECTOR{'S' if self._unstable != 1 else ''} ━━━[/]")
                for s in result.unstable:
                    self._log(f"  [yellow]▒[/] Sector {s.index:>3d}  0x{s.address:06X}  CRC 0x{s.crc32:08X}")

        self._log(
            f"\n[dim]Completed in {elapsed:.1f}s  "
            f"({self._good} good, {self._empty} empty, "
            f"{self._unstable} unstable, {self._bad} bad)[/]"
        )

        self.query_one("#scan-btn", Button).disabled = False
        if self._good > 0 or self._unstable > 0:
            self.query_one("#save-btn", Button).disabled = False

        self.post_message(self.ScanComplete())

    async def _save_recoverable_data(self) -> None:
        from defib.agent.client import FlashAgentClient, ScanResult, SectorStatus

        scan_result: ScanResult = self._scan_result  # type: ignore[assignment]
        client: FlashAgentClient = self._client  # type: ignore[assignment]
        if scan_result is None or client is None:
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"flash_recovery_{timestamp}.bin"

        readable = [
            s for s in scan_result.sectors
            if s.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE)
        ]

        self._log(f"\n[bright_cyan]Saving recoverable data to {filename}...[/]")
        self._log(f"  Reading {len(readable)} sectors ({len(readable) * self._sector_size // 1024}KB)...")

        flash_base = 0x14000000
        recoverable = bytearray()

        for i, sector in enumerate(scan_result.sectors):
            if sector.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE):
                data = await client.read_memory(
                    flash_base + sector.address, self._sector_size, fast=True,
                )
                recoverable.extend(data)
                if (i + 1) % 16 == 0 or i == len(scan_result.sectors) - 1:
                    done = sum(
                        1 for s in scan_result.sectors[:i + 1]
                        if s.status in (SectorStatus.GOOD, SectorStatus.UNSTABLE)
                    )
                    self._log(f"  [dim]{done}/{len(readable)} sectors read[/]")
            else:
                recoverable.extend(b"\xFF" * self._sector_size)

        from pathlib import Path
        Path(filename).write_bytes(recoverable)
        self._log(f"[green bold]Saved:[/] {filename} ({len(recoverable) // 1024}KB)")
        self.notify(f"Saved {filename}", severity="information", title="Flash Data Saved")

    # ── Utilities ────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        try:
            log = self.query_one("#results-log", RichLog)
            ts = datetime.now().strftime("%H:%M:%S")
            log.write(f"[dim]{ts}[/] {message}")
        except Exception:
            pass

    def _disconnect(self) -> None:
        if self._transport is not None:
            from defib.transport.base import Transport
            transport: Transport = self._transport  # type: ignore[assignment]
            asyncio.ensure_future(transport.close())
            self._transport = None
            self._client = None
