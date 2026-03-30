"""Cross-platform temporary static IP address management.

Assigns and removes temporary static IP addresses on network interfaces
for TFTP-based device recovery. The device typically expects the host
at a specific IP (e.g., 192.168.1.10).

Platform support:
- Linux: ip addr add/del
- macOS: ifconfig alias
- Windows: netsh interface ip add/delete address
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class IPManagerError(Exception):
    """Failed to manage IP address."""


async def _run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Run a shell command asynchronously and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def add_ip(interface: str, ip: str, netmask: str = "255.255.255.0") -> None:
    """Add a temporary static IP address to a network interface.

    Args:
        interface: Network interface name (e.g., "eth0", "en0", "Ethernet").
        ip: IP address to assign (e.g., "192.168.1.10").
        netmask: Subnet mask (default "255.255.255.0").

    Raises:
        IPManagerError: If the command fails.
    """
    prefix = _netmask_to_prefix(netmask)

    if sys.platform == "linux":
        cmd = ["ip", "addr", "add", f"{ip}/{prefix}", "dev", interface]
    elif sys.platform == "darwin":
        cmd = ["ifconfig", interface, "alias", ip, "netmask", netmask]
    elif sys.platform == "win32":
        cmd = ["netsh", "interface", "ip", "add", "address", interface, ip, netmask]
    else:
        raise IPManagerError(f"Unsupported platform: {sys.platform}")

    logger.info("Adding IP %s/%d to %s: %s", ip, prefix, interface, " ".join(cmd))
    returncode, stdout, stderr = await _run_command(cmd)

    if returncode != 0:
        raise IPManagerError(
            f"Failed to add IP {ip} to {interface}: {stderr.strip() or stdout.strip()}"
        )
    logger.info("Successfully added %s to %s", ip, interface)


async def remove_ip(interface: str, ip: str, netmask: str = "255.255.255.0") -> None:
    """Remove a temporary static IP address from a network interface.

    Args:
        interface: Network interface name.
        ip: IP address to remove.
        netmask: Subnet mask.
    """
    prefix = _netmask_to_prefix(netmask)

    if sys.platform == "linux":
        cmd = ["ip", "addr", "del", f"{ip}/{prefix}", "dev", interface]
    elif sys.platform == "darwin":
        cmd = ["ifconfig", interface, "-alias", ip]
    elif sys.platform == "win32":
        cmd = ["netsh", "interface", "ip", "delete", "address", interface, ip]
    else:
        raise IPManagerError(f"Unsupported platform: {sys.platform}")

    logger.info("Removing IP %s from %s: %s", ip, interface, " ".join(cmd))
    returncode, stdout, stderr = await _run_command(cmd)

    if returncode != 0:
        logger.warning("Failed to remove IP %s from %s: %s", ip, interface, stderr.strip())
    else:
        logger.info("Successfully removed %s from %s", ip, interface)


@asynccontextmanager
async def temporary_ip(
    interface: str,
    ip: str,
    netmask: str = "255.255.255.0",
) -> AsyncIterator[str]:
    """Context manager that assigns a temporary IP and removes it on exit.

    Usage:
        async with temporary_ip("eth0", "192.168.1.10") as ip:
            # ip is now assigned to eth0
            await do_tftp_recovery()
        # ip is automatically removed

    Args:
        interface: Network interface name.
        ip: IP address to assign temporarily.
        netmask: Subnet mask.

    Yields:
        The assigned IP address.
    """
    await add_ip(interface, ip, netmask)
    try:
        yield ip
    finally:
        await remove_ip(interface, ip, netmask)


def _netmask_to_prefix(netmask: str) -> int:
    """Convert dotted netmask to CIDR prefix length."""
    parts = netmask.split(".")
    if len(parts) != 4:
        return 24
    binary = "".join(f"{int(p):08b}" for p in parts)
    return binary.count("1")


def list_interfaces() -> list[str]:
    """List available network interfaces.

    Returns interface names that can be used with add_ip/remove_ip.
    """
    import socket

    interfaces: list[str] = []
    try:
        if hasattr(socket, "if_nameindex"):
            for _, name in socket.if_nameindex():
                if name != "lo":
                    interfaces.append(name)
    except OSError:
        pass

    if not interfaces:
        # Fallback: common defaults
        if sys.platform == "linux":
            interfaces = ["eth0", "enp0s3"]
        elif sys.platform == "darwin":
            interfaces = ["en0", "en1"]
        elif sys.platform == "win32":
            interfaces = ["Ethernet", "Wi-Fi"]

    return interfaces
