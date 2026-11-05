"""The TFTP server resends what the client has not ACKed.

Novatek's mask ROM and Eth loader send each ACK once and then wait for
good; a block lost or held on the way must not hang the recovery.
"""

import asyncio
import struct

from defib.network.tftp_server import (
    OPCODE_ACK,
    OPCODE_DATA,
    OPCODE_OACK,
    OPCODE_RRQ,
    MAX_TRANSFERS,
    TFTPServerProtocol,
)
from tests.test_network import MockDatagramTransport

ADDR = ("10.0.0.2", 2962)
# Checks fall half a timeout between resends: Windows timers are coarse
TIMEOUT = 0.2


async def _after(resends: int) -> None:
    """Wait until resend number `resends` has gone out, and no further."""
    await asyncio.sleep(TIMEOUT * (resends + 0.5))


def _server(data: bytes, retries: int = 3) -> tuple[TFTPServerProtocol, MockDatagramTransport]:
    protocol = TFTPServerProtocol(
        data, retransmit_timeout=TIMEOUT, max_retries=retries,
    )
    transport = MockDatagramTransport()
    protocol.connection_made(transport)  # type: ignore[arg-type]
    return protocol, transport


def _blocks(transport: MockDatagramTransport) -> list[int]:
    return [
        struct.unpack("!H", pkt[2:4])[0]
        for pkt, _ in transport.sent
        if struct.unpack("!H", pkt[:2])[0] == OPCODE_DATA
    ]


def _rrq(options: bytes = b"") -> bytes:
    return struct.pack("!H", OPCODE_RRQ) + b"loader.bin\x00octet\x00" + options


def _ack(block: int) -> bytes:
    return struct.pack("!HH", OPCODE_ACK, block)


async def test_unacked_block_is_resent() -> None:
    protocol, transport = _server(b"x" * 1000)
    protocol.datagram_received(_rrq(), ADDR)
    await _after(2)
    assert _blocks(transport) == [1, 1, 1]
    assert transport.sent[1][0] == transport.sent[0][0]


async def test_ack_stops_the_resend_and_the_transfer_goes_on() -> None:
    protocol, transport = _server(b"x" * 1000)
    protocol.datagram_received(_rrq(), ADDR)
    await _after(1)
    protocol.datagram_received(_ack(1), ADDR)
    protocol.datagram_received(_ack(2), ADDR)
    await _after(2)
    assert _blocks(transport) == [1, 1, 2]
    assert protocol.stats.transfers_complete == 1


async def test_duplicate_ack_sends_nothing() -> None:
    """The held block and its retransmit both arrive and are both ACKed."""
    protocol, transport = _server(b"x" * 1500)
    protocol.datagram_received(_rrq(), ADDR)
    await _after(1)
    protocol.datagram_received(_ack(1), ADDR)
    protocol.datagram_received(_ack(1), ADDR)
    assert _blocks(transport) == [1, 1, 2]


async def test_oack_is_resent() -> None:
    protocol, transport = _server(b"x" * 3000)
    protocol.datagram_received(_rrq(b"blksize\x001408\x00"), ADDR)
    await _after(1)
    opcodes = [struct.unpack("!H", pkt[:2])[0] for pkt, _ in transport.sent]
    assert opcodes == [OPCODE_OACK, OPCODE_OACK]
    protocol.datagram_received(_ack(0), ADDR)
    assert _blocks(transport) == [1]


async def test_gives_up_after_max_retries() -> None:
    protocol, transport = _server(b"x" * 1000, retries=2)
    protocol.datagram_received(_rrq(), ADDR)
    await _after(4)
    assert _blocks(transport) == [1, 1, 1]
    protocol.datagram_received(_ack(1), ADDR)
    assert _blocks(transport) == [1, 1, 1]
    assert protocol.stats.transfers_complete == 0


async def test_last_block_is_resent_too() -> None:
    protocol, transport = _server(b"x" * 100)
    protocol.datagram_received(_rrq(), ADDR)
    await _after(1)
    protocol.datagram_received(_ack(1), ADDR)
    await _after(1)
    assert _blocks(transport) == [1, 1]
    assert protocol.stats.transfers_complete == 1


async def test_close_cancels_pending_resends() -> None:
    protocol, transport = _server(b"x" * 1000)
    protocol.datagram_received(_rrq(), ADDR)
    protocol.connection_lost(None)
    await _after(1)
    assert _blocks(transport) == [1]


async def test_requests_past_the_cap_get_nothing() -> None:
    """RRQs come unauthenticated: a flood of them, forged or not, cannot
    make the server send (and resend) to any number of addresses."""
    protocol, transport = _server(b"x" * 1000)
    for i in range(MAX_TRANSFERS + 3):
        protocol.datagram_received(_rrq(), ("10.0.0.%d" % (i + 10), 2962))
    assert len(transport.sent) == MAX_TRANSFERS
    protocol.connection_lost(None)
