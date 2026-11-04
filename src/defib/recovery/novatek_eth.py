"""Novatek recovery over Ethernet: the mask ROM's own auto-boot path.

With no valid loader in flash, the ROM goes NOR, SD, then Ethernet (NT9856x
ROM code Application Note, 1.3 and 1.4.3): DHCP, then a TFTP fetch of
``loader.bin`` from the offer's next-server. That is the only source a
board with its straps at flash reaches on a network, and the one XM boards
are left with. defib answers both ends:

1. DHCP, only for the 00:00:00:xx:xx:xx MACs the ROM and the loader use
   (a fresh one each boot), so a network DHCP server can stay up: the first
   offer wins, and ours goes out at once.
2. TFTP: ``loader.bin``, the SDK loader built ``STORAGEEXT=Eth``, which
   brings the DDR up, does its own DHCP and fetches ``FW98<part>T.BIN``, the
   pack of device tree and U-Boot, and starts U-Boot from RAM.

Nothing is written to flash. The UART only carries the console: Ctrl-C stops
U-Boot's autoboot at its prompt.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from defib.network.dhcp_server import start_dhcp_server
from defib.network.tftp_server import start_tftp_server
from defib.power.base import PowerController
from defib.protocol.base import ProtocolError
from defib.protocol.novatek import load_recovery_set
from defib.recovery.events import LogEvent, RecoveryResult, Stage
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

ETH_LOADER = "loader.bin"
# The ROM's TFTP waits for ever once it has a lease: a board that is not
# fetching within this long is not on this link, or has a valid loader
FETCH_TIMEOUT = 90.0
PROMPTS = (b"OpenIPC #", b"=> ")


def pack_name(chip: str) -> str:
    """The pack the Ethernet loader asks for: FW98562T.BIN for nt98562."""
    return f"FW{chip.split(':', 1)[0].lower().removeprefix('nt')}T.BIN"


def is_novatek_boot_mac(mac: bytes) -> bool:
    """The ROM and the loader both use 00:00:00 and three random bytes."""
    return mac[:3] == b"\0\0\0"


async def _catch_prompt(transport: Transport, timeout: float) -> tuple[bool, bytes]:
    """Ctrl-C until U-Boot's prompt shows, past its autoboot."""
    seen = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await transport.write(b"\x03")
        try:
            seen += await transport.read(4096, timeout=0.05)
        except TransportTimeout:
            continue
        if any(p in seen for p in PROMPTS):
            await transport.write(b"\r")
            return True, bytes(seen)
    return False, bytes(seen)


async def recover_over_ethernet(
    chip: str,
    archive: bytes,
    nic: str,
    host_ip: str,
    device_ip: str,
    flash_type: str = "nor",
    transport: Transport | None = None,
    power: PowerController | None = None,
    poe_port: str | None = None,
    on_log: Callable[[LogEvent], None] | None = None,
    fetch_timeout: float = FETCH_TIMEOUT,
) -> RecoveryResult:
    def log(message: str, level: str = "info") -> None:
        logger.log(logging.ERROR if level == "error" else logging.INFO, message)
        if on_log:
            on_log(LogEvent(level=level, message=message))

    start = time.monotonic()

    def failed(error: str, done: list[Stage]) -> RecoveryResult:
        log(error, "error")
        return RecoveryResult(
            success=False, stages_completed=done, error=error,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )

    try:
        loader, pack = load_recovery_set(archive, chip, flash_type, loader=ETH_LOADER)
    except ProtocolError as e:
        return failed(str(e), [])
    name = pack_name(chip)

    try:
        dhcp, _ = await start_dhcp_server(
            nic, host_ip, device_ip, ETH_LOADER, is_novatek_boot_mac,
        )
    except (OSError, ValueError) as e:
        return failed(f"DHCP: {e}", [])
    try:
        tftp, served = await start_tftp_server(
            bind_addr=host_ip, files={ETH_LOADER: loader, name: pack}, done_file=name,
        )
    except Exception as e:
        dhcp.close()
        return failed(f"TFTP: {e}", [])
    log(f"Serving {ETH_LOADER} and {name} on {nic} ({host_ip}); the board gets {device_ip}")

    try:
        if power is not None and poe_port is not None:
            log("Power-cycling the board")
            await power.power_cycle(poe_port)
        else:
            log("Waiting for the board: power it on now")
        if not await served.wait_for_completion(timeout=fetch_timeout):
            return failed(
                f"The board fetched nothing in {fetch_timeout:.0f} s: is it on {nic}, "
                "with no valid loader in flash?",
                [],
            )
        log(f"{ETH_LOADER} and {name} sent; U-Boot is starting from RAM")
        done = [Stage.SPL, Stage.UBOOT]
        if transport is not None:
            ok, seen = await _catch_prompt(transport, timeout=15.0)
            if not ok:
                tail = seen[-200:].decode("latin1", "replace")
                return failed(
                    f"No U-Boot prompt on the console after the pack; it showed {tail!r}",
                    done,
                )
            log("U-Boot prompt")
        done.append(Stage.COMPLETE)
        return RecoveryResult(
            success=True, stages_completed=done,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )
    finally:
        tftp.close()
        dhcp.close()
