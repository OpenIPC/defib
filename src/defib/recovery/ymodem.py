"""Minimal async YMODEM sender for vendor U-Boot chainloading."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from defib.transport.base import Transport, TransportTimeout

SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = 0x43
CPMEOF = 0x1A

ControlLog = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


class YModemError(RuntimeError):
    """YMODEM transfer failed."""


@dataclass(frozen=True)
class YModemStats:
    bytes_sent: int
    data_packets: int
    retries: int


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def make_packet(sequence: int, payload: bytes) -> bytes:
    if len(payload) == 128:
        marker = SOH
    elif len(payload) == 1024:
        marker = STX
    else:
        raise ValueError("YMODEM payload must be exactly 128 or 1024 bytes")
    seq = sequence & 0xFF
    crc = crc16_xmodem(payload)
    return bytes((marker, seq, 0xFF - seq)) + payload + crc.to_bytes(2, "big")


def make_header(filename: str, size: int) -> bytes:
    try:
        name = filename.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("YMODEM filename must be ASCII") from exc
    if b"\x00" in name:
        raise ValueError("YMODEM filename must not contain NUL")
    metadata = name + b"\x00" + str(size).encode("ascii") + b"\x00"
    if len(metadata) > 128:
        raise ValueError("YMODEM metadata does not fit block 0")
    return make_packet(0, metadata.ljust(128, b"\x00"))


class YModemSender:
    def __init__(
        self,
        transport: Transport,
        *,
        control_timeout: float = 5.0,
        start_timeout: float = 15.0,
        packet_retries: int = 32,
        on_log: ControlLog | None = None,
    ) -> None:
        self._transport = transport
        self._control_timeout = control_timeout
        self._start_timeout = start_timeout
        self._packet_retries = packet_retries
        self._on_log = on_log
        self._retries = 0

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            self._on_log(message)

    async def _read_control(self, allowed: set[int], timeout: float) -> int:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        stray = 0
        last_stray: int | None = None

        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                data = await self._transport.read(1, timeout=remaining)
            except TransportTimeout:
                break
            if not data:
                continue
            value = data[0]
            if value in allowed:
                if stray:
                    self._log(
                        f"YMODEM ignored {stray} stray byte(s) before 0x{value:02X}"
                    )
                return value
            stray += 1
            last_stray = value

        suffix = ""
        if stray and last_stray is not None:
            suffix = f" after {stray} stray byte(s), last=0x{last_stray:02X}"
        raise YModemError(f"timed out waiting for YMODEM control byte{suffix}")

    async def _wait_crc_request(self, timeout: float) -> None:
        value = await self._read_control({CRC_REQUEST, CAN}, timeout)
        if value == CAN:
            raise YModemError("YMODEM receiver cancelled transfer")

    async def _send_packet(self, packet: bytes, label: str) -> None:
        for attempt in range(1, self._packet_retries + 1):
            try:
                # Discard anything still buffered before every attempt, the
                # way HiSiliconStandard._send_frame_with_retry does.  A stalled
                # drain leaves the packet queued, so a retransmission can put
                # two copies on the wire and draw two responses; without this
                # the spare response is read as the NEXT packet's answer and a
                # NAK the receiver actually sent is silently treated as an ACK.
                await self._transport.flush_input()
                # write() and flush_output() are inside the try because both
                # block on the TX queue and raise TransportTimeout when it
                # stalls.  A hung adapter should cost one retry, not abort the
                # chainload with a raw transport error.
                await self._transport.write(packet)
                await self._transport.flush_output()
                value = await self._read_control(
                    {ACK, NAK, CRC_REQUEST, CAN}, self._control_timeout
                )
            except (YModemError, TransportTimeout):
                value = -1

            if value == ACK:
                return
            if value == CAN:
                raise YModemError(f"receiver cancelled during {label}")

            self._retries += 1
            if value >= 0:
                self._log(
                    f"YMODEM {label}: retry {attempt}/{self._packet_retries} "
                    f"after 0x{value:02X}"
                )
            else:
                self._log(
                    f"YMODEM {label}: retry {attempt}/{self._packet_retries} "
                    "after timeout"
                )

        raise YModemError(f"{label} not ACKed after {self._packet_retries} attempts")

    async def _finish(self) -> None:
        # The last data packet may have been retransmitted, leaving a spare
        # response behind that would otherwise be read as the EOT answer.
        await self._transport.flush_input()
        await self._transport.write(bytes((EOT,)))
        await self._transport.flush_output()
        first = await self._read_control(
            {ACK, NAK, CAN, CRC_REQUEST}, self._control_timeout
        )
        if first == CAN:
            raise YModemError("receiver cancelled at EOT")

        if first == NAK:
            await self._transport.write(bytes((EOT,)))
            await self._transport.flush_output()
            second = await self._read_control(
                {ACK, CAN, NAK, CRC_REQUEST}, self._control_timeout
            )
            if second == CAN:
                raise YModemError("receiver cancelled at second EOT")
            if second != ACK:
                raise YModemError(
                    f"receiver did not ACK second EOT (got 0x{second:02X})"
                )
            await self._wait_crc_request(self._control_timeout)
        elif first == ACK:
            await self._wait_crc_request(self._control_timeout)
        elif first != CRC_REQUEST:
            raise YModemError(f"unexpected EOT response 0x{first:02X}")

        await self._send_packet(make_packet(0, bytes(128)), "final header")

    async def send(
        self,
        data: bytes,
        *,
        filename: str = "u-boot.bin",
        on_progress: ProgressCallback | None = None,
    ) -> YModemStats:
        self._retries = 0
        await self._wait_crc_request(self._start_timeout)
        await self._send_packet(make_header(filename, len(data)), "header")
        await self._wait_crc_request(self._control_timeout)

        sent = 0
        sequence = 1
        packets = 0
        for offset in range(0, len(data), 1024):
            chunk = data[offset : offset + 1024]
            useful = len(chunk)
            payload = chunk.ljust(1024, bytes((CPMEOF,)))
            await self._send_packet(
                make_packet(sequence, payload), f"data #{sequence}"
            )
            sent += useful
            packets += 1
            if on_progress is not None:
                on_progress(sent, len(data))
            sequence = (sequence + 1) & 0xFF

        await self._finish()
        return YModemStats(sent, packets, self._retries)
