"""Shared U-Boot TFTP command sequencing.

The transport policy belongs to the caller: dump/restore can provide a lenient
runner, while install provides a strict status-preserving runner. This module
only owns command spelling, tftpboot→tftp fallback, and transfer-result parsing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

CommandResultRunner = Callable[[str, float], Awaitable[tuple[bool, str]]]


def _reports_unknown_command(response: str, command_name: str) -> bool:
    """Match U-Boot's Unknown command result for the command just attempted."""

    expected = command_name.lower()
    for raw_line in response.splitlines():
        line = raw_line.strip().lower()
        if not line.startswith("unknown command"):
            continue
        if expected in line or line == "unknown command":
            return True
    return False


def uboot_tftp_commands(
    filename: str,
    ram_addr: int,
    *,
    use_loadaddr: bool,
) -> tuple[str, str]:
    """Return primary/fallback U-Boot TFTP commands for one staged file."""

    if use_loadaddr:
        return f"tftpboot {filename}", f"tftp {filename}"
    address = f"0x{ram_addr:x}"
    return f"tftpboot {address} {filename}", f"tftp {address} {filename}"


async def run_uboot_tftp(
    run_command: CommandResultRunner,
    filename: str,
    ram_addr: int,
    *,
    use_loadaddr: bool,
    timeout: float = 120.0,
) -> str:
    """Fetch one file into RAM using the caller's command-completion policy."""

    primary, fallback = uboot_tftp_commands(
        filename,
        ram_addr,
        use_loadaddr=use_loadaddr,
    )
    ok, response = await run_command(primary, timeout)
    if _reports_unknown_command(response, primary.split()[0]):
        ok, response = await run_command(fallback, timeout)

    if not ok:
        detail = response.strip()[-200:] or "<no response>"
        raise RuntimeError(f"TFTP command failed or timed out: {detail}")

    text = response.lower()
    if "done" not in text and "bytes transferred" not in text:
        raise RuntimeError(f"TFTP download failed: {response.strip()[-200:]}")

    return response
