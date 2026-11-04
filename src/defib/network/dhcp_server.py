"""A minimal DHCP server for boot ROMs that load over TFTP.

It answers only the clients a predicate picks by MAC, so it can run on a
network that has its own DHCP server, and it answers at once and by
broadcast: a ROM takes the first offer it can read, and one with no IP yet
drops a unicast reply. Every client gets the same address, the server as
next-server and a boot file name; DISCOVER gets OFFER, REQUEST gets ACK.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import sys
from collections.abc import Callable

logger = logging.getLogger(__name__)

SERVER_PORT = 67
CLIENT_PORT = 68
MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, ACK = 1, 2, 3, 5
LEASE_SECONDS = 600


def _options(data: bytes) -> dict[int, bytes]:
    opts: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        size = data[i + 1]
        opts[code] = data[i + 2:i + 2 + size]
        i += 2 + size
    return opts


def build_reply(
    request: bytes, kind: int, client_ip: str, server_ip: str, netmask: str,
    bootfile: str,
) -> bytes:
    """The OFFER or ACK to a BOOTREQUEST."""
    head = bytearray(240)
    head[0:4] = b"\x02\x01\x06\x00"            # BOOTREPLY, Ethernet, 6-byte MAC
    head[4:8] = request[4:8]                    # xid
    struct.pack_into("!H", head, 10, 0x8000)   # broadcast
    head[16:20] = socket.inet_aton(client_ip)  # yiaddr
    head[20:24] = socket.inet_aton(server_ip)  # siaddr: the TFTP server
    head[28:44] = request[28:44]                # chaddr
    name = bootfile.encode()[:127]
    head[108:108 + len(name)] = name
    head[236:240] = MAGIC
    opts = (
        bytes([53, 1, kind])
        + bytes([54, 4]) + socket.inet_aton(server_ip)
        + bytes([51, 4]) + struct.pack("!I", LEASE_SECONDS)
        + bytes([1, 4]) + socket.inet_aton(netmask)
        + b"\xff"
    )
    return bytes(head) + opts


class DHCPServerProtocol(asyncio.DatagramProtocol):
    def __init__(
        self, client_ip: str, server_ip: str, netmask: str, bootfile: str,
        accept: Callable[[bytes], bool],
    ) -> None:
        self._client_ip = client_ip
        self._server_ip = server_ip
        self._netmask = netmask
        self._bootfile = bootfile
        self._accept = accept
        self._transport: asyncio.DatagramTransport | None = None
        self.leases: list[str] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 240 or data[0] != 1 or data[236:240] != MAGIC:
            return
        mac = data[28:34]
        if not self._accept(mac):
            return
        kind = _options(data[240:]).get(53, b"\0")[0]
        reply = {DISCOVER: OFFER, REQUEST: ACK}.get(kind)
        if reply is None or self._transport is None:
            return
        self._transport.sendto(
            build_reply(
                data, reply, self._client_ip, self._server_ip, self._netmask,
                self._bootfile,
            ),
            ("255.255.255.255", CLIENT_PORT),
        )
        if reply == ACK:
            self.leases.append(mac.hex(":"))
        logger.info(
            "DHCP %s %s -> %s", "ACK" if reply == ACK else "OFFER",
            mac.hex(":"), self._client_ip,
        )


async def start_dhcp_server(
    nic: str, server_ip: str, client_ip: str, bootfile: str,
    accept: Callable[[bytes], bool], netmask: str = "255.255.255.0",
) -> tuple[asyncio.DatagramTransport, DHCPServerProtocol]:
    """Answer the clients ``accept`` picks by MAC, on ``nic``.

    Port 67 needs root. On Linux the socket is tied to ``nic``, so it shares
    the port with any other DHCP server bound to its own interface."""
    if ipaddress.ip_address(client_ip) not in ipaddress.ip_network(
        f"{server_ip}/{netmask}", strict=False,
    ):
        raise ValueError(f"{client_ip} is not on {server_ip}'s network")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if sys.platform.startswith("linux"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, nic.encode())
    try:
        sock.bind(("", SERVER_PORT))
    except OSError as exc:
        sock.close()
        raise OSError(
            f"cannot listen for DHCP on port {SERVER_PORT} ({exc}); it needs root"
        ) from exc
    sock.setblocking(False)
    protocol = DHCPServerProtocol(client_ip, server_ip, netmask, bootfile, accept)
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: protocol, sock=sock,
    )
    assert isinstance(transport, asyncio.DatagramTransport)
    return transport, protocol
