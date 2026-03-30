"""UDP broadcast discovery for devices in U-Boot network boot mode.

Many HiSilicon devices broadcast ARP requests or respond to specific
UDP packets when in U-Boot network recovery mode. This module provides
utilities to discover such devices on the local network.

Common U-Boot network patterns:
- Device sends ARP requests for its configured serverip
- Device may respond to BOOTP/DHCP broadcasts
- Device sends TFTP requests to serverip
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 4011  # Custom discovery port
BROADCAST_ADDR = "255.255.255.255"
ARP_LISTEN_TIMEOUT = 10.0


@dataclass
class DiscoveredDevice:
    """A device found during network discovery."""
    ip: str
    mac: str | None = None
    hostname: str | None = None
    info: str = ""


class DiscoveryProtocol(asyncio.DatagramProtocol):
    """UDP protocol for receiving broadcast responses from devices."""

    def __init__(self) -> None:
        self.devices: list[DiscoveredDevice] = []
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        ip = addr[0]
        logger.debug("Received %d bytes from %s:%d", len(data), ip, addr[1])

        # Check for known U-Boot response patterns
        device = DiscoveredDevice(ip=ip, info=f"Response from {ip}:{addr[1]}")

        # Avoid duplicates
        if not any(d.ip == ip for d in self.devices):
            self.devices.append(device)
            logger.info("Discovered device: %s", ip)


async def discover_devices(
    interface_ip: str = "0.0.0.0",
    timeout: float = ARP_LISTEN_TIMEOUT,
    broadcast_port: int = DISCOVERY_PORT,
) -> list[DiscoveredDevice]:
    """Listen for devices broadcasting on the network.

    This sends a broadcast probe and listens for any responses.
    Works best when the device is powered on in U-Boot network mode.

    Args:
        interface_ip: IP of the network interface to use.
        timeout: Seconds to listen for responses.
        broadcast_port: UDP port for broadcast discovery.

    Returns:
        List of discovered devices.
    """
    loop = asyncio.get_running_loop()
    protocol = DiscoveryProtocol()

    # Create a UDP socket with broadcast enabled
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(interface_ip, 0),
        allow_broadcast=True,
    )

    try:
        # Send a broadcast probe
        probe = b"DEFIB_DISCOVER\x00"
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        transport.sendto(probe, (BROADCAST_ADDR, broadcast_port))
        logger.info("Sent discovery broadcast on port %d", broadcast_port)

        # Listen for responses
        await asyncio.sleep(timeout)

    finally:
        transport.close()

    return protocol.devices


async def scan_arp_table() -> list[DiscoveredDevice]:
    """Parse the system ARP table for recently seen devices.

    This is a passive discovery method — it finds devices that have
    recently communicated on the local network.
    """
    import subprocess
    import sys

    devices: list[DiscoveredDevice] = []

    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["arp", "-a"], capture_output=True, text=True, timeout=5
            )
        else:
            result = subprocess.run(
                ["arp", "-an"], capture_output=True, text=True, timeout=5
            )

        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                # Try to extract IP and MAC
                for part in parts:
                    if part.startswith("(") and part.endswith(")"):
                        ip = part[1:-1]
                        mac = parts[parts.index(part) + 2] if parts.index(part) + 2 < len(parts) else None
                        devices.append(DiscoveredDevice(ip=ip, mac=mac, info="ARP table entry"))
                        break
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.debug("ARP table scan failed", exc_info=True)

    return devices
