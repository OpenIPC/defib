"""Async TFTP server for network-based firmware recovery.

Implements RFC 1350 (TFTP) with RFC 2348 (blocksize option) using
asyncio.DatagramProtocol. Serves a single file for device download.

Typical U-Boot TFTP flow:
1. Device sends RRQ (read request) for a filename
2. Server responds with DATA blocks (512 bytes default, or negotiated)
3. Device ACKs each block
4. Transfer ends when a DATA block < blocksize is sent
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# TFTP opcodes
OPCODE_RRQ = 1
OPCODE_WRQ = 2
OPCODE_DATA = 3
OPCODE_ACK = 4
OPCODE_ERROR = 5
OPCODE_OACK = 6  # Option Acknowledgment (RFC 2347)

# TFTP error codes
ERR_NOT_DEFINED = 0
ERR_FILE_NOT_FOUND = 1
ERR_ACCESS_VIOLATION = 2
ERR_DISK_FULL = 3
ERR_ILLEGAL_OP = 4
ERR_UNKNOWN_TID = 5
ERR_FILE_EXISTS = 6
ERR_NO_SUCH_USER = 7

DEFAULT_BLOCKSIZE = 512
DEFAULT_PORT = 69
DEFAULT_TIMEOUT = 5.0
MAX_RETRIES = 5


@dataclass
class TFTPTransfer:
    """State for an active TFTP transfer."""
    addr: tuple[str, int]
    data: bytes
    blocksize: int = DEFAULT_BLOCKSIZE
    block_num: int = 0
    complete: bool = False
    retries: int = 0


@dataclass
class TFTPServerStats:
    """Statistics for a TFTP server session."""
    requests: int = 0
    bytes_sent: int = 0
    transfers_complete: int = 0
    errors: int = 0


class TFTPServerProtocol(asyncio.DatagramProtocol):
    """Async TFTP server protocol.

    Serves a single file to any client that requests it via RRQ.
    """

    def __init__(
        self,
        file_data: bytes,
        filename: str = "firmware.bin",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self._file_data = file_data
        self._filename = filename
        self._on_progress = on_progress
        self._transfers: dict[tuple[str, int], TFTPTransfer] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self.stats = TFTPServerStats()
        self._done_event = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 2:
            return

        opcode = struct.unpack("!H", data[:2])[0]

        if opcode == OPCODE_RRQ:
            self._handle_rrq(data, addr)
        elif opcode == OPCODE_ACK:
            self._handle_ack(data, addr)
        elif opcode == OPCODE_ERROR:
            self._handle_error(data, addr)
        else:
            self._send_error(addr, ERR_ILLEGAL_OP, "Unsupported operation")

    def _handle_rrq(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle Read Request."""
        self.stats.requests += 1

        # Parse RRQ: opcode(2) + filename + \0 + mode + \0 [+ options]
        try:
            parts = data[2:].split(b"\x00")
            filename = parts[0].decode("ascii")
            mode = parts[1].decode("ascii").lower() if len(parts) > 1 else "octet"
        except (IndexError, UnicodeDecodeError):
            self._send_error(addr, ERR_NOT_DEFINED, "Malformed RRQ")
            return

        logger.info("TFTP RRQ from %s:%d for '%s' (mode=%s)", addr[0], addr[1], filename, mode)

        # Parse options (RFC 2347)
        blocksize = DEFAULT_BLOCKSIZE
        options = {}
        i = 2
        while i < len(parts) - 1:
            opt_name = parts[i].decode("ascii", errors="replace").lower()
            opt_value = parts[i + 1].decode("ascii", errors="replace")
            options[opt_name] = opt_value
            i += 2

        if "blksize" in options:
            try:
                requested_bs = int(options["blksize"])
                blocksize = max(8, min(requested_bs, 65464))
            except ValueError:
                pass

        transfer = TFTPTransfer(addr=addr, data=self._file_data, blocksize=blocksize)
        self._transfers[addr] = transfer

        # Send OACK if options were negotiated
        if options and blocksize != DEFAULT_BLOCKSIZE:
            oack = struct.pack("!H", OPCODE_OACK)
            oack += b"blksize\x00" + str(blocksize).encode() + b"\x00"
            self._send(addr, oack)
            logger.debug("Sent OACK with blksize=%d", blocksize)
        else:
            # Send first data block
            self._send_data_block(transfer)

    def _handle_ack(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle ACK for a data block."""
        if len(data) < 4:
            return

        block_num = struct.unpack("!H", data[2:4])[0]
        transfer = self._transfers.get(addr)

        if transfer is None:
            return

        if block_num == 0 and transfer.block_num == 0:
            # ACK for OACK — send first data block
            self._send_data_block(transfer)
            return

        if block_num == transfer.block_num:
            transfer.retries = 0

            if self._on_progress:
                sent = min(transfer.block_num * transfer.blocksize, len(transfer.data))
                self._on_progress(sent, len(transfer.data))

            if transfer.complete:
                self.stats.transfers_complete += 1
                self.stats.bytes_sent += len(transfer.data)
                logger.info("Transfer complete to %s:%d (%d bytes)", addr[0], addr[1], len(transfer.data))
                del self._transfers[addr]
                self._done_event.set()
                return

            # Send next block
            self._send_data_block(transfer)

    def _handle_error(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle error from client."""
        self.stats.errors += 1
        if len(data) >= 4:
            error_code = struct.unpack("!H", data[2:4])[0]
            error_msg = data[4:].rstrip(b"\x00").decode("ascii", errors="replace")
            logger.warning("TFTP error from %s:%d: code=%d msg=%s", addr[0], addr[1], error_code, error_msg)

        if addr in self._transfers:
            del self._transfers[addr]

    def _send_data_block(self, transfer: TFTPTransfer) -> None:
        """Send the next data block for a transfer."""
        transfer.block_num += 1
        offset = (transfer.block_num - 1) * transfer.blocksize
        block_data = transfer.data[offset:offset + transfer.blocksize]

        packet = struct.pack("!HH", OPCODE_DATA, transfer.block_num) + block_data
        self._send(transfer.addr, packet)

        if len(block_data) < transfer.blocksize:
            transfer.complete = True

    def _send_error(self, addr: tuple[str, int], code: int, msg: str) -> None:
        """Send a TFTP error packet."""
        self.stats.errors += 1
        packet = struct.pack("!HH", OPCODE_ERROR, code) + msg.encode("ascii") + b"\x00"
        self._send(addr, packet)

    def _send(self, addr: tuple[str, int], data: bytes) -> None:
        if self._transport:
            self._transport.sendto(data, addr)

    async def wait_for_completion(self, timeout: float = 300.0) -> bool:
        """Wait for at least one transfer to complete."""
        try:
            await asyncio.wait_for(self._done_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


async def start_tftp_server(
    file_data: bytes,
    bind_addr: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    filename: str = "firmware.bin",
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[asyncio.DatagramTransport, TFTPServerProtocol]:
    """Start an async TFTP server.

    Args:
        file_data: The firmware data to serve.
        bind_addr: Address to bind to.
        port: UDP port (default 69, needs root on Linux).
        filename: Filename to advertise (metadata only).
        on_progress: Callback(bytes_sent, bytes_total) for progress.

    Returns:
        Tuple of (transport, protocol) for the running server.
    """
    loop = asyncio.get_running_loop()
    protocol = TFTPServerProtocol(file_data, filename, on_progress)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=(bind_addr, port),
    )

    logger.info("TFTP server listening on %s:%d (serving %d bytes)", bind_addr, port, len(file_data))
    assert isinstance(transport, asyncio.DatagramTransport)
    return transport, protocol
