"""Tests for network recovery components."""

import struct


from defib.network.tftp_server import (
    TFTPServerProtocol,
    OPCODE_RRQ,
    OPCODE_ACK,
    OPCODE_DATA,
    OPCODE_ERROR,
    DEFAULT_BLOCKSIZE,
)
from defib.network.ip_manager import _netmask_to_prefix, list_interfaces
from defib.network.discovery import DiscoveredDevice


class TestTFTPServerProtocol:
    def test_create_protocol(self):
        protocol = TFTPServerProtocol(b"\x00" * 1024)
        assert protocol.stats.requests == 0

    def test_handle_rrq(self):
        """RRQ should start a transfer."""
        protocol = TFTPServerProtocol(b"hello world")
        transport = MockDatagramTransport()
        protocol.connection_made(transport)

        # Build an RRQ packet: opcode(2) + filename + \0 + mode + \0
        rrq = struct.pack("!H", OPCODE_RRQ) + b"firmware.bin\x00octet\x00"
        addr = ("192.168.1.100", 12345)
        protocol.datagram_received(rrq, addr)

        assert protocol.stats.requests == 1
        assert len(transport.sent) == 1
        # First response should be DATA block 1
        data = transport.sent[0][0]
        opcode = struct.unpack("!H", data[:2])[0]
        assert opcode == OPCODE_DATA

    def test_full_transfer_small_file(self):
        """Complete transfer of a small file (< 512 bytes)."""
        file_data = b"Hello, TFTP!" * 10  # 120 bytes
        protocol = TFTPServerProtocol(file_data)
        transport = MockDatagramTransport()
        protocol.connection_made(transport)
        addr = ("192.168.1.100", 54321)

        # RRQ
        rrq = struct.pack("!H", OPCODE_RRQ) + b"test.bin\x00octet\x00"
        protocol.datagram_received(rrq, addr)

        # Should get DATA block 1 with all file data (< 512 bytes = last block)
        assert len(transport.sent) == 1
        data_pkt = transport.sent[0][0]
        opcode = struct.unpack("!H", data_pkt[:2])[0]
        block = struct.unpack("!H", data_pkt[2:4])[0]
        payload = data_pkt[4:]

        assert opcode == OPCODE_DATA
        assert block == 1
        assert payload == file_data

        # ACK block 1
        ack = struct.pack("!HH", OPCODE_ACK, 1)
        protocol.datagram_received(ack, addr)

        assert protocol.stats.transfers_complete == 1
        assert protocol.stats.bytes_sent == len(file_data)

    def test_multi_block_transfer(self):
        """Transfer of a file that spans multiple blocks."""
        file_data = bytes(range(256)) * 5  # 1280 bytes = 3 blocks at 512
        protocol = TFTPServerProtocol(file_data)
        transport = MockDatagramTransport()
        protocol.connection_made(transport)
        addr = ("10.0.0.1", 9999)

        # RRQ
        rrq = struct.pack("!H", OPCODE_RRQ) + b"fw.bin\x00octet\x00"
        protocol.datagram_received(rrq, addr)

        # Block 1 (512 bytes)
        assert len(transport.sent) == 1
        pkt = transport.sent[0][0]
        assert struct.unpack("!H", pkt[2:4])[0] == 1
        assert len(pkt[4:]) == DEFAULT_BLOCKSIZE

        # ACK 1 → get block 2
        protocol.datagram_received(struct.pack("!HH", OPCODE_ACK, 1), addr)
        assert len(transport.sent) == 2
        pkt2 = transport.sent[1][0]
        assert struct.unpack("!H", pkt2[2:4])[0] == 2
        assert len(pkt2[4:]) == DEFAULT_BLOCKSIZE

        # ACK 2 → get block 3 (256 bytes, last block)
        protocol.datagram_received(struct.pack("!HH", OPCODE_ACK, 2), addr)
        assert len(transport.sent) == 3
        pkt3 = transport.sent[2][0]
        assert struct.unpack("!H", pkt3[2:4])[0] == 3
        assert len(pkt3[4:]) == 256  # < 512, last block

        # ACK 3 → transfer complete
        protocol.datagram_received(struct.pack("!HH", OPCODE_ACK, 3), addr)
        assert protocol.stats.transfers_complete == 1

    def test_blocksize_option(self):
        """RRQ with blksize option should negotiate larger blocks."""
        file_data = b"\x00" * 2048
        protocol = TFTPServerProtocol(file_data)
        transport = MockDatagramTransport()
        protocol.connection_made(transport)
        addr = ("10.0.0.1", 1234)

        # RRQ with blksize option
        rrq = struct.pack("!H", OPCODE_RRQ)
        rrq += b"fw.bin\x00octet\x00blksize\x001024\x00"
        protocol.datagram_received(rrq, addr)

        # Should get OACK first
        assert len(transport.sent) == 1
        oack = transport.sent[0][0]
        assert struct.unpack("!H", oack[:2])[0] == 6  # OACK opcode
        assert b"blksize" in oack
        assert b"1024" in oack

        # ACK 0 (OACK acknowledgment) → get DATA block 1
        protocol.datagram_received(struct.pack("!HH", OPCODE_ACK, 0), addr)
        assert len(transport.sent) == 2
        pkt = transport.sent[1][0]
        assert len(pkt[4:]) == 1024  # Negotiated blocksize

    def test_error_response(self):
        """Client error should be handled gracefully."""
        protocol = TFTPServerProtocol(b"data")
        transport = MockDatagramTransport()
        protocol.connection_made(transport)
        addr = ("10.0.0.1", 5555)

        # Start a transfer
        rrq = struct.pack("!H", OPCODE_RRQ) + b"fw.bin\x00octet\x00"
        protocol.datagram_received(rrq, addr)

        # Client sends error
        err = struct.pack("!HH", OPCODE_ERROR, 0) + b"abort\x00"
        protocol.datagram_received(err, addr)
        assert protocol.stats.errors == 1

    def test_progress_callback(self):
        """Progress callback should be called during transfer."""
        progress_calls: list[tuple[int, int]] = []
        file_data = b"\x00" * 1024  # 2 blocks

        protocol = TFTPServerProtocol(
            file_data,
            on_progress=lambda sent, total: progress_calls.append((sent, total)),
        )
        transport = MockDatagramTransport()
        protocol.connection_made(transport)
        addr = ("10.0.0.1", 8888)

        rrq = struct.pack("!H", OPCODE_RRQ) + b"fw.bin\x00octet\x00"
        protocol.datagram_received(rrq, addr)

        # ACK block 1
        protocol.datagram_received(struct.pack("!HH", OPCODE_ACK, 1), addr)
        assert len(progress_calls) >= 1


class TestIPManager:
    def test_netmask_to_prefix_24(self):
        assert _netmask_to_prefix("255.255.255.0") == 24

    def test_netmask_to_prefix_16(self):
        assert _netmask_to_prefix("255.255.0.0") == 16

    def test_netmask_to_prefix_32(self):
        assert _netmask_to_prefix("255.255.255.255") == 32

    def test_netmask_to_prefix_invalid(self):
        assert _netmask_to_prefix("invalid") == 24

    def test_list_interfaces(self):
        interfaces = list_interfaces()
        assert isinstance(interfaces, list)
        # Should return at least one interface on any system
        assert len(interfaces) >= 1


class TestDiscovery:
    def test_discovered_device(self):
        device = DiscoveredDevice(ip="192.168.1.100", mac="aa:bb:cc:dd:ee:ff")
        assert device.ip == "192.168.1.100"
        assert device.mac == "aa:bb:cc:dd:ee:ff"


class TestNetworkCLI:
    def test_network_help(self):
        from typer.testing import CliRunner
        from defib.cli.app import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["network", "--help"])
        assert result.exit_code == 0
        assert "--file" in result.stdout
        assert "--nic" in result.stdout
        assert "--tftp-port" in result.stdout

    def test_list_interfaces_cmd(self):
        import json
        from typer.testing import CliRunner
        from defib.cli.app import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, ["list-interfaces", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "interfaces" in data

    def test_network_missing_file(self):
        from typer.testing import CliRunner
        from defib.cli.app import app as cli_app
        runner = CliRunner()
        result = runner.invoke(cli_app, [
            "network", "-f", "/tmp/nonexistent_defib_file.bin", "--skip-ip"
        ])
        assert result.exit_code != 0


# --- Test helpers ---

class MockDatagramTransport:
    """Mock asyncio.DatagramTransport for TFTP protocol testing."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int] | None = None) -> None:
        if addr:
            self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default
