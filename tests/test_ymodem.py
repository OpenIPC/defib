from __future__ import annotations

import pytest

from defib.recovery.ymodem import (
    ACK,
    CRC_REQUEST,
    EOT,
    NAK,
    SOH,
    STX,
    YModemSender,
    crc16_xmodem,
    make_header,
    make_packet,
)
from defib.transport.base import Transport, TransportTimeout


def test_crc16_xmodem_reference_vector():
    assert crc16_xmodem(b"123456789") == 0x31C3


def test_make_1k_packet():
    payload = bytes(range(256)) * 4
    packet = make_packet(7, payload)
    assert packet[0] == STX
    assert packet[1:3] == bytes((7, 0xF8))
    assert len(packet) == 3 + 1024 + 2


def test_make_header_is_block_zero():
    packet = make_header("u-boot.bin", 182580)
    assert packet[0] == SOH
    assert packet[1:3] == b"\x00\xff"
    assert b"u-boot.bin\x00182580\x00" in packet


class ScriptedYModemReceiver(Transport):
    """Minimal receiver state machine for sender protocol regression tests."""

    def __init__(self, *, nak_first_data: bool = False) -> None:
        self.rx = bytearray((CRC_REQUEST,))
        self.tx: list[bytes] = []
        self.nak_first_data = nak_first_data
        self.header_seen = False
        self.data_attempts: dict[int, int] = {}
        self.eot_count = 0

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if not self.rx:
            raise TransportTimeout("scripted YMODEM receiver is idle")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def write(self, data: bytes) -> None:
        self.tx.append(bytes(data))
        if data == bytes((EOT,)):
            self.eot_count += 1
            if self.eot_count == 1:
                self.rx.extend((NAK,))
            else:
                self.rx.extend((ACK, CRC_REQUEST))
            return

        if not data or data[0] not in (SOH, STX):
            return

        sequence = data[1]
        if sequence == 0:
            if not self.header_seen:
                self.header_seen = True
                self.rx.extend((ACK, CRC_REQUEST))
            else:
                # Empty final header terminates the batch.
                self.rx.extend((ACK,))
            return

        attempts = self.data_attempts.get(sequence, 0) + 1
        self.data_attempts[sequence] = attempts
        if self.nak_first_data and sequence == 1 and attempts == 1:
            self.rx.extend((NAK,))
        else:
            self.rx.extend((ACK,))

    async def flush_input(self) -> None:
        self.rx.clear()

    async def flush_output(self) -> None:
        pass

    async def bytes_waiting(self) -> int:
        return len(self.rx)


@pytest.mark.asyncio
async def test_sender_happy_path_runs_header_data_eot_and_final_header():
    transport = ScriptedYModemReceiver()
    payload = b"U" * 1500
    sender = YModemSender(
        transport,
        control_timeout=0.05,
        start_timeout=0.05,
        packet_retries=4,
    )

    stats = await sender.send(payload, filename="u-boot.bin")

    assert stats.bytes_sent == len(payload)
    assert stats.data_packets == 2
    assert stats.retries == 0
    assert transport.eot_count == 2
    assert transport.data_attempts == {1: 1, 2: 1}
    block_zero_packets = [
        packet
        for packet in transport.tx
        if packet and packet[0] == SOH and len(packet) > 2 and packet[1] == 0
    ]
    assert len(block_zero_packets) == 2  # file header + empty final header


@pytest.mark.asyncio
async def test_sender_retries_one_nak_then_completes_handshake():
    transport = ScriptedYModemReceiver(nak_first_data=True)
    sender = YModemSender(
        transport,
        control_timeout=0.05,
        start_timeout=0.05,
        packet_retries=4,
    )

    stats = await sender.send(b"U" * 512, filename="u-boot.bin")

    assert stats.bytes_sent == 512
    assert stats.data_packets == 1
    assert stats.retries == 1
    assert transport.data_attempts == {1: 2}
    assert transport.eot_count == 2


class StalledTxReceiver(ScriptedYModemReceiver):
    """Receiver whose TX queue refuses to drain for the first data packets.

    A stalled queue means the packet never fully reached the receiver, so the
    stalled attempt is recorded but deliberately left unacknowledged.
    """

    def __init__(self, *, stall_attempts: int = 2) -> None:
        super().__init__()
        self.stall_attempts = stall_attempts
        self.stalls = 0
        self._stall_this_flush = False

    async def write(self, data: bytes) -> None:
        is_data = bool(data) and data[0] in (SOH, STX) and data[1] != 0
        if is_data and self.stalls < self.stall_attempts:
            self.stalls += 1
            self._stall_this_flush = True
            self.tx.append(bytes(data))
            return
        await super().write(data)

    async def flush_output(self) -> None:
        if self._stall_this_flush:
            self._stall_this_flush = False
            raise TransportTimeout("simulated TX queue stall")


@pytest.mark.asyncio
async def test_sender_retries_a_stalled_tx_queue_instead_of_aborting():
    """A TX-queue stall must cost one retry, not abort the transfer.

    SerialTransport.flush_output() waits for queued bytes to drain and raises
    TransportTimeout when they do not.  It runs on every packet attempt, so if
    it sits outside the retry try block a hung USB-UART aborts the chainload
    with a raw transport error instead of retrying.
    """
    transport = StalledTxReceiver(stall_attempts=2)
    sender = YModemSender(transport, control_timeout=0.05, start_timeout=0.05)

    stats = await sender.send(b"payload", filename="u-boot.bin")

    assert transport.stalls == 2, "the stall must actually have been exercised"
    assert stats.bytes_sent == len(b"payload")
    assert stats.retries >= 2, "each stall should cost one retry, not the transfer"


class DuplicatingStalledReceiver(Transport):
    """Holds a stalled packet, then delivers both copies once the link returns.

    Models a USB-UART whose TX queue stalls: ``flush_output()`` times out with
    the packet still queued, the sender retransmits, and when the link recovers
    the receiver sees that packet twice and answers twice.
    """

    def __init__(self, *, stall_seq: int, nak_seq: int) -> None:
        self.rx = bytearray((CRC_REQUEST,))
        self.stall_seq = stall_seq
        self.nak_seq = nak_seq
        self.held: int | None = None
        self.header_seen = False
        self.seen: list[int] = []
        self.naked_once = False

    def _respond(self, sequence: int) -> None:
        self.seen.append(sequence)
        if sequence == 0:
            if not self.header_seen:
                self.header_seen = True
                self.rx.extend((ACK, CRC_REQUEST))
            else:
                self.rx.extend((ACK,))
            return
        if sequence == self.nak_seq and not self.naked_once:
            self.naked_once = True
            self.rx.extend((NAK,))
        else:
            self.rx.extend((ACK,))

    async def write(self, data: bytes) -> None:
        if data == bytes((EOT,)):
            self.rx.extend((ACK, CRC_REQUEST))
            return
        if not data or data[0] not in (SOH, STX):
            return
        sequence = data[1]
        if sequence == self.stall_seq and self.held is None:
            self.held = sequence
            return
        if self.held is not None:
            self._respond(self.held)
            self.held = None
        self._respond(sequence)

    async def flush_output(self) -> None:
        if self.held is not None:
            raise TransportTimeout("TX queue still holding a packet")

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        if not self.rx:
            raise TransportTimeout("scripted receiver is idle")
        data = bytes(self.rx[:size])
        del self.rx[:size]
        return data

    async def flush_input(self) -> None:
        self.rx.clear()

    async def bytes_waiting(self) -> int:
        return len(self.rx)

    async def unread(self, data: bytes) -> None:
        self.rx = bytearray(data) + self.rx

    async def close(self) -> None:
        pass

    async def set_baudrate(self, baud: int) -> None:
        pass


@pytest.mark.asyncio
async def test_duplicate_response_cannot_acknowledge_a_later_packet():
    """A spare response must never be attributed to the next packet.

    Retransmitting after a stalled drain can put two copies of one packet on
    the wire, drawing two responses. If the extra one survives into the next
    packet's read window it masks that packet's real answer -- a NAK reads as
    an ACK and the receiver never gets the data it asked to have resent.
    """
    receiver = DuplicatingStalledReceiver(stall_seq=1, nak_seq=2)
    sender = YModemSender(
        receiver, control_timeout=0.05, start_timeout=0.05, packet_retries=6
    )

    await sender.send(b"A" * 2048, filename="u-boot.bin")

    assert receiver.naked_once, "the scenario must actually exercise a NAK"
    assert receiver.seen.count(2) >= 2, (
        "a NAKed packet must be retransmitted, not covered by a stale ACK"
    )
