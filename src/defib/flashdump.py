"""Flash dump via U-Boot serial console.

Reads the entire SPI/NAND flash contents through U-Boot's memory
display command (md.b) and saves as a binary file.

Flow:
1. sf probe 0         → detect SPI flash type and size
2. sf read ADDR 0 SZ  → read flash into RAM
3. md.b ADDR SZ       → hex dump RAM over serial
4. Parse hex output   → binary file

The md.b output format:
    82000000: ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff    ................
    82000010: 48 69 53 69 6c 69 63 6f 6e 00 00 00 00 00 00 00    HiSilico........
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

# Default RAM address for flash readout
DEFAULT_RAM_ADDR = 0x82000000
# Common SPI flash sizes (bytes)
FLASH_SIZES = {
    "8MB": 0x800000,
    "16MB": 0x1000000,
    "32MB": 0x2000000,
}
# How long to wait for sf read to complete (large flash can be slow)
SF_READ_TIMEOUT = 30.0
# Regex for md.b output line
MD_LINE_RE = re.compile(
    r"^([0-9a-fA-F]{8}):\s+"
    r"((?:[0-9a-fA-F]{2}[\s]+){0,15}[0-9a-fA-F]{2})"
)


def parse_md_line(line: str) -> tuple[int, bytes] | None:
    """Parse a single md.b output line into (address, data).

    Returns None if the line doesn't match the expected format.

    Example input:
        "82000000: ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff    ................"
    Returns:
        (0x82000000, b'\\xff' * 16)
    """
    m = MD_LINE_RE.match(line.strip())
    if not m:
        return None
    addr = int(m.group(1), 16)
    hex_bytes = m.group(2).strip().split()
    data = bytes(int(b, 16) for b in hex_bytes)
    return addr, data


def parse_md_output(text: str) -> bytes:
    """Parse complete md.b output into binary data.

    Handles multi-line output, ignoring non-data lines.
    Lines are sorted by address to handle out-of-order output.
    """
    entries: list[tuple[int, bytes]] = []
    for line in text.splitlines():
        result = parse_md_line(line)
        if result:
            entries.append(result)

    if not entries:
        return b""

    entries.sort(key=lambda e: e[0])
    base_addr = entries[0][0]
    # Calculate total size from address range
    last_addr, last_data = entries[-1]
    total_size = (last_addr - base_addr) + len(last_data)

    buf = bytearray(total_size)
    for addr, data in entries:
        offset = addr - base_addr
        buf[offset:offset + len(data)] = data

    return bytes(buf)


async def send_command(
    transport: Transport,
    cmd: str,
    timeout: float = 5.0,
    wait_for: str | None = None,
) -> str:
    """Send a command to U-Boot and collect the response.

    Args:
        transport: Serial transport with U-Boot console active.
        cmd: Command string to send.
        timeout: Max seconds to wait for response.
        wait_for: Optional string to wait for in response (e.g., prompt).

    Returns:
        The collected response text.
    """
    # Clear any pending input
    try:
        avail = await transport.bytes_waiting()
        if avail > 0:
            await transport.read(avail, timeout=0.1)
    except Exception:
        pass

    await transport.write((cmd + "\r").encode())
    buf = bytearray()
    start = time.monotonic()
    idle_count = 0

    while time.monotonic() - start < timeout:
        try:
            avail = await transport.bytes_waiting()
            if avail > 0:
                data = await transport.read(min(avail, 4096), timeout=0.5)
                buf.extend(data)
                idle_count = 0
                if wait_for:
                    # Check only the tail for prompt to avoid false matches
                    # (e.g., '#' appears in md.b hex ASCII column)
                    tail = buf[-64:].decode("ascii", errors="replace")
                    if wait_for in tail:
                        return buf.decode("ascii", errors="replace")
            else:
                idle_count += 1
                # If we have data and serial has been idle for a bit,
                # the command is likely done (no prompt-based detection)
                if not wait_for and len(buf) > 0 and idle_count > 10:
                    return buf.decode("ascii", errors="replace")
                import asyncio
                await asyncio.sleep(0.05)
        except TransportTimeout:
            continue

    return buf.decode("ascii", errors="replace")


def detect_flash_from_text(text: str) -> int | None:
    """Parse flash size from any U-Boot output text.

    Recognizes patterns from boot log and sf probe output:
      "Chip:16MB"
      "SPI Nor total size: 16MB"
      "total size: 8MB"
      "0x1000000" (in flash-related context)
    """
    # Look for flash size patterns like "Chip:16MB", "total size: 16MB"
    # Use word-boundary-aware regex to avoid matching "128MB" as "8MB"
    # Search for patterns in flash-related context (near "chip", "flash", "size", "nor")
    for m in re.finditer(r"(?:chip|flash|size|nor)[^0-9]*?(\d+)\s*mb", text, re.IGNORECASE):
        mb = int(m.group(1))
        if mb in (4, 8, 16, 32, 64):
            return mb * 1024 * 1024

    # Fallback: any standalone NMB where N is a valid flash size
    for m in re.finditer(r"\b(\d+)\s*MB\b", text):
        mb = int(m.group(1))
        if mb in (4, 8, 16, 32, 64):
            return mb * 1024 * 1024

    # Try hex sizes — find all matches and pick valid flash sizes
    for hex_match in re.finditer(r"0x([0-9a-fA-F]{6,8})", text):
        try:
            size = int(hex_match.group(1), 16)
            # Must be a power-of-two flash size (1MB, 2MB, 4MB, 8MB, 16MB, 32MB, 64MB)
            if 0x100000 <= size <= 0x4000000 and (size & (size - 1)) == 0:
                return size
        except ValueError:
            pass

    return None


async def detect_flash(
    transport: Transport,
    on_log: Callable[[str], None] | None = None,
    boot_log: str = "",
) -> int | None:
    """Detect SPI flash size.

    First checks the boot log (already captured by the console reader),
    then falls back to running sf probe 0.

    Args:
        transport: Serial transport with U-Boot console active.
        on_log: Callback for log messages.
        boot_log: Previously captured boot/console output to search first.

    Returns flash size in bytes, or None if detection fails.
    """
    # First: check boot log we already have
    if boot_log:
        size = detect_flash_from_text(boot_log)
        if size:
            if on_log:
                on_log(f"Flash size from boot log: {size // (1024*1024)}MB")
            return size

    # Second: try sf probe 0
    if on_log:
        on_log("Probing SPI flash...")

    resp = await send_command(transport, "sf probe 0", timeout=5.0, wait_for="# ")
    size = detect_flash_from_text(resp)
    if size:
        if on_log:
            on_log(f"Detected flash: {size // (1024*1024)}MB")
        return size

    if on_log:
        on_log("Could not detect flash size automatically")
    return None


async def dump_flash(
    transport: Transport,
    output_path: str,
    flash_size: int | None = None,
    ram_addr: int = DEFAULT_RAM_ADDR,
    on_progress: Callable[[int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    boot_log: str = "",
) -> int:
    """Dump entire flash to a binary file via U-Boot md.b command.

    Args:
        transport: Serial transport with U-Boot console active.
        output_path: Path to save the binary dump.
        flash_size: Flash size in bytes (auto-detected if None).
        ram_addr: RAM address to load flash contents into.
        on_progress: Callback(bytes_done, bytes_total).
        on_log: Callback for log messages.

    Returns:
        Number of bytes dumped.

    Raises:
        RuntimeError: If flash detection or reading fails.
    """

    # Detect flash size if not provided
    if flash_size is None:
        flash_size = await detect_flash(transport, on_log, boot_log=boot_log)
        if flash_size is None:
            raise RuntimeError(
                "Could not detect flash size. Specify manually with --size."
            )

    size_mb = flash_size / (1024 * 1024)
    if on_log:
        on_log(f"Reading {size_mb:.0f}MB flash into RAM at 0x{ram_addr:08x}...")

    # Read flash into RAM
    cmd = f"sf read 0x{ram_addr:x} 0x0 0x{flash_size:x}"
    resp = await send_command(transport, cmd, timeout=SF_READ_TIMEOUT, wait_for="# ")

    if "OK" not in resp and "ok" not in resp.lower():
        # Try anyway — some U-Boot versions don't print OK
        if on_log:
            on_log("Warning: sf read response unclear, proceeding with dump")

    if on_log:
        on_log(f"Dumping {size_mb:.0f}MB via md.b (this takes a while)...")

    # Dump via md.b in chunks to show progress
    # md.b can handle large sizes, but we chunk for progress reporting
    chunk_size = 0x10000  # 64KB chunks
    offset = 0

    with open(output_path, "wb") as f:
        while offset < flash_size:
            remaining = min(chunk_size, flash_size - offset)
            addr = ram_addr + offset
            cmd = f"md.b 0x{addr:x} 0x{remaining:x}"

            # md.b output can be large — give it time
            resp = await send_command(transport, cmd, timeout=60.0, wait_for="# ")
            chunk_data = parse_md_output(resp)

            if len(chunk_data) == 0:
                if on_log:
                    on_log(f"Warning: no data parsed at offset 0x{offset:x}")
                # Write zeros for missing data
                chunk_data = b"\x00" * remaining

            f.write(chunk_data)
            offset += len(chunk_data)

            if on_progress:
                on_progress(offset, flash_size)

    if on_log:
        on_log(f"Flash dump saved: {output_path} ({offset} bytes)")

    return offset
