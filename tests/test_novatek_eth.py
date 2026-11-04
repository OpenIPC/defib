"""Novatek recovery over Ethernet: the DHCP that answers the mask ROM."""

import socket
import struct

from defib.network.dhcp_server import (
    ACK,
    DISCOVER,
    MAGIC,
    OFFER,
    REQUEST,
    DHCPServerProtocol,
    _options,
)
from defib.recovery.novatek_eth import is_novatek_boot_mac, pack_name

ROM_MAC = bytes.fromhex("000000c5e2d9")


def bootrequest(kind: int, mac: bytes = ROM_MAC) -> bytes:
    head = bytearray(240)
    head[0:4] = b"\x01\x01\x06\x00"
    head[4:8] = b"\x55\x1c\x99\x42"
    head[28:34] = mac
    head[236:240] = MAGIC
    return bytes(head) + bytes([53, 1, kind, 255])


class Sent:
    def __init__(self) -> None:
        self.packets: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.packets.append((data, addr))


def server() -> tuple[DHCPServerProtocol, Sent]:
    proto = DHCPServerProtocol(
        "10.0.0.251", "10.0.0.229", "255.255.255.0", "loader.bin", is_novatek_boot_mac,
    )
    sent = Sent()
    proto._transport = sent  # type: ignore[assignment]
    return proto, sent


def test_discover_gets_a_broadcast_offer_naming_us_and_the_loader():
    proto, sent = server()
    proto.datagram_received(bootrequest(DISCOVER), ("0.0.0.0", 68))
    (reply, addr), = sent.packets
    assert addr == ("255.255.255.255", 68)
    assert reply[4:8] == b"\x55\x1c\x99\x42"
    assert struct.unpack_from("!H", reply, 10)[0] == 0x8000
    assert socket.inet_ntoa(reply[16:20]) == "10.0.0.251"
    assert socket.inet_ntoa(reply[20:24]) == "10.0.0.229"
    assert reply[28:34] == ROM_MAC
    assert reply[108:118] == b"loader.bin"
    opts = _options(reply[240:])
    assert opts[53] == bytes([OFFER])
    assert socket.inet_ntoa(opts[54]) == "10.0.0.229"


def test_request_gets_an_ack():
    proto, sent = server()
    proto.datagram_received(bootrequest(REQUEST), ("0.0.0.0", 68))
    assert _options(sent.packets[0][0][240:])[53] == bytes([ACK])
    assert proto.leases == ["00:00:00:c5:e2:d9"]


def test_other_clients_are_left_to_the_network():
    proto, sent = server()
    proto.datagram_received(bootrequest(DISCOVER, bytes.fromhex("305a3a531b3f")), ("0.0.0.0", 68))
    proto.datagram_received(b"junk", ("0.0.0.0", 68))
    assert not sent.packets


def test_the_pack_name():
    assert pack_name("nt98562") == "FW98562T.BIN"
    assert pack_name("NT98323:x") == "FW98323T.BIN"


def test_a_request_to_another_server_is_left_alone():
    proto, sent = server()
    request = bootrequest(REQUEST)[:-1] + bytes([54, 4]) + socket.inet_aton("10.0.0.1") + b"\xff"
    proto.datagram_received(request, ("0.0.0.0", 68))
    assert not sent.packets


def test_the_server_the_rom_took_comes_off_its_console():
    from defib.recovery.novatek_eth import offer_server

    console = b"S1RI\r\naOfIP 0AD8807F SRV 0AD88001\r\nMSK FFFFFF00\r\nbS2"
    assert offer_server(console) == "10.216.128.1"
    assert offer_server(b"NPIp>K9S1K2HE") is None


def test_the_latest_offer_counts():
    from defib.recovery.novatek_eth import offer_server

    console = b"OfIP 0AD8807F SRV 0AD88001\r\n...OfIP C0A84DFB SRV C0A84D01\r\n"
    assert offer_server(console) == "192.168.77.1"
