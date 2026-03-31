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

# RAM base addresses by SoC family (from qemu-hisilicon/hw/arm/hisilicon.c)
# Used to derive safe staging address for flash readout via md.b
RAM_BASE: dict[str, int] = {
    # 0x80000000 family
    "hi3516cv100": 0x80000000,
    "hi3516cv200": 0x80000000,
    "hi3516av100": 0x80000000,
    "hi3516av200": 0x80000000,
    "hi3516cv300": 0x80000000,
    "hi3516cv500": 0x80000000,
    "hi3519v101": 0x80000000,
    "hi3516a": 0x80000000,
    "hi3516dv100": 0x80000000,
    "hi3516dv300": 0x80000000,
    "hi3518": 0x80000000,
    "hi3518ev200": 0x80000000,
    "hi3520d": 0x80000000,
    "hi3531": 0x80000000,
    "hi3531a": 0x80000000,
    "hi3535": 0x80000000,
    "hi3536": 0x80000000,
    "hi3519": 0x80000000,
    "hi3559v100": 0x80000000,
    # 0x40000000 family
    "hi3516ev200": 0x40000000,
    "hi3516ev300": 0x40000000,
    "hi3518ev300": 0x40000000,
    "hi3516dv200": 0x40000000,
    "gk7205v200": 0x40000000,
    "gk7205v300": 0x40000000,
    "gk7202v300": 0x40000000,
    "gk7605v100": 0x40000000,
    "gk7205v500": 0x40000000,
    "hi3516cv608": 0x40000000,
    "hi3516cv610": 0x40000000,
    "hi3516cv613": 0x40000000,
}
# Offset from RAM base for flash staging area (leaves room for U-Boot)
RAM_STAGING_OFFSET = 0x2000000  # 32MB into RAM
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


def get_ram_staging_addr(chip: str) -> int:
    """Get safe RAM address for flash staging based on SoC type.

    Looks up the chip's RAM base from the known table, falling back to
    the profile's U-Boot load address if not found.
    """
    chip_lower = chip.lower()
    # Direct match
    if chip_lower in RAM_BASE:
        return RAM_BASE[chip_lower] + RAM_STAGING_OFFSET
    # Try resolving via profile (some chips alias to others)
    try:
        from defib.profiles.loader import load_profile
        profile = load_profile(chip_lower)
        uboot_addr = int(profile.addresses[2], 16)
        # Derive RAM base from U-Boot address (aligned to 0x40000000 boundary)
        ram_base = uboot_addr & 0xF0000000
        return ram_base + RAM_STAGING_OFFSET
    except Exception:
        pass
    # Last resort: use profile name for lookup
    for prefix, base in RAM_BASE.items():
        if chip_lower.startswith(prefix[:8]):
            return base + RAM_STAGING_OFFSET
    # Default for unknown chips
    return 0x82000000


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


async def _detect_crc32(transport: Transport) -> bool:
    """Check if U-Boot has the crc32 command."""
    resp = await send_command(transport, "crc32 0 0", timeout=3.0, wait_for="# ")
    # If crc32 exists, it will output a CRC value or usage.
    # If not, it will say "Unknown command"
    return "unknown command" not in resp.lower()


async def _get_device_crc32(
    transport: Transport, addr: int, size: int
) -> int | None:
    """Run crc32 on the device and parse the result.

    U-Boot crc32 output: "CRC32 for 42000000 ... 42000fff ==> abcd1234"
    """
    cmd = f"crc32 0x{addr:x} 0x{size:x}"
    resp = await send_command(transport, cmd, timeout=5.0, wait_for="# ")
    # Parse "==> XXXXXXXX" pattern
    m = re.search(r"==>\s*([0-9a-fA-F]{8})", resp)
    if m:
        return int(m.group(1), 16)
    return None


async def dump_flash(
    transport: Transport,
    output_path: str,
    flash_size: int | None = None,
    ram_addr: int | None = None,
    chip: str = "",
    on_progress: Callable[[int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_stats: Callable[[dict[str, object]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
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

    # Resolve RAM staging address
    if ram_addr is None:
        if chip:
            ram_addr = get_ram_staging_addr(chip)
        else:
            ram_addr = 0x82000000  # Legacy default, may crash on 0x40000000 boards
    if on_log:
        on_log(f"RAM staging address: 0x{ram_addr:08x}")

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
        if on_log:
            on_log("Warning: sf read response unclear, proceeding with dump")

    # Sanity check: read first 16 bytes and verify they look like flash content.
    # Valid SPI NOR flash starts with ARM vectors (0xEA branch opcodes) or
    # a bootloader header — never all-zeros, all-0xFF, or all-0x55.
    check_resp = await send_command(
        transport, f"md.b 0x{ram_addr:x} 10", timeout=5.0, wait_for="# ",
    )
    check_data = parse_md_output(check_resp)
    if len(check_data) >= 16:
        # Detect garbage patterns
        unique = len(set(check_data[:16]))
        all_same = unique == 1
        if all_same:
            bad_byte = check_data[0]
            raise RuntimeError(
                f"Flash read sanity check failed: first 16 bytes are all "
                f"0x{bad_byte:02x}. This usually means sf probe/sf read "
                f"failed silently. Try running 'sf probe 0' manually first."
            )
        if on_log:
            on_log(f"Sanity check OK: first bytes {check_data[:4].hex()}")

    # Detect if U-Boot has crc32 command for per-block verification
    has_crc32 = await _detect_crc32(transport)
    if on_log:
        if has_crc32:
            on_log("CRC32 verification enabled")
        else:
            on_log("CRC32 not available — dumping without verification")

    if on_log:
        on_log(f"Dumping {size_mb:.0f}MB via md.b...")

    chunk_size = 0x1000  # 4KB chunks
    max_retries = 3
    offset = 0
    verified = 0
    errors = 0
    retries_total = 0
    start_time = time.monotonic()

    with open(output_path, "wb") as f:
        while offset < flash_size:
            remaining = min(chunk_size, flash_size - offset)
            addr = ram_addr + offset

            for attempt in range(max_retries + 1):
                cmd = f"md.b 0x{addr:x} 0x{remaining:x}"
                resp = await send_command(transport, cmd, timeout=15.0, wait_for="# ")
                chunk_data = parse_md_output(resp)

                if len(chunk_data) == 0:
                    if attempt < max_retries:
                        if on_log:
                            on_log(f"Retry {attempt + 1}: no data at 0x{offset:x}")
                        retries_total += 1
                        continue
                    if on_log:
                        on_log(f"Warning: no data at offset 0x{offset:x}, writing zeros")
                    chunk_data = b"\x00" * remaining
                    break

                # CRC32 verification if available
                if has_crc32 and len(chunk_data) == remaining:
                    import zlib
                    local_crc = zlib.crc32(chunk_data) & 0xFFFFFFFF
                    device_crc = await _get_device_crc32(
                        transport, addr, remaining
                    )
                    if device_crc is not None and device_crc != local_crc:
                        errors += 1
                        if attempt < max_retries:
                            if on_log:
                                retries_total += 1
                                if on_log is not None:
                                    on_log(
                                        f"CRC mismatch at 0x{offset:x} "
                                        f"(local={local_crc:08x} device={device_crc:08x}), "
                                        f"retry {attempt + 1}"
                                    )
                            continue
                        if on_log:
                            on_log(f"CRC mismatch at 0x{offset:x} after {max_retries} retries")
                    else:
                        verified += 1
                break

            f.write(chunk_data)
            offset += len(chunk_data)

            if on_progress:
                on_progress(offset, flash_size)

            # Check for cancellation between blocks
            if is_cancelled is not None and is_cancelled():
                if on_log:
                    on_log(f"Cancelled at {offset // 1024}KB / {flash_size // 1024}KB")
                break

            if on_stats:
                elapsed = time.monotonic() - start_time
                total_blocks = (flash_size + chunk_size - 1) // chunk_size
                blocks_done = offset // chunk_size
                bps = offset / elapsed if elapsed > 0 else 0
                eta = (flash_size - offset) / bps if bps > 0 else 0
                on_stats({
                    "blocks_done": blocks_done,
                    "blocks_total": total_blocks,
                    "verified": verified,
                    "errors": errors,
                    "retries": retries_total,
                    "elapsed_s": elapsed,
                    "bytes_per_s": bps,
                    "eta_s": eta,
                    "crc_enabled": has_crc32,
                })

    if on_log:
        msg = f"Flash dump saved: {output_path} ({offset} bytes)"
        if has_crc32:
            total_blocks = (flash_size + chunk_size - 1) // chunk_size
            msg += f" — {verified}/{total_blocks} blocks verified"
            if errors:
                msg += f", {errors} errors"
        on_log(msg)

    return offset
