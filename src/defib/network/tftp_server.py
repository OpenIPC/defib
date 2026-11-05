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
import errno
import logging
import struct
import sys
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


class TFTPBindError(Exception):
    """The TFTP server could not take the address it was asked to serve on.

    Raised instead of the bare OSError so the caller can tell the operator
    what to do about it: the kernel's own text ("Address already in use")
    names neither the address nor the fix, and this failure lands in the
    middle of a flash recovery where guessing is expensive.
    """


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
MAX_BLOCKSIZE = 65464
DEFAULT_PORT = 69
# The server resends its last packet (DATA or OACK) when no ACK comes back
# in time: RFC 1350 leaves that to both ends, and some clients never do
# it. Novatek's mask ROM and Eth loader send each ACK once and then wait
# for good, so one block lost or held on the way (a switch port bridged in
# software was seen holding frames for up to 2 s) hangs the recovery
# unless the server resends it.
RETRANSMIT_TIMEOUT = 1.0
MAX_RETRIES = 10
# Transfers served at once. A recovery runs one or two; RRQs are not
# authenticated, so past this a request (possibly forged) is not answered
MAX_TRANSFERS = 8


@dataclass
class TFTPTransfer:
    """State for an active TFTP transfer."""
    addr: tuple[str, int]
    data: bytes
    filename: str = ""
    blocksize: int = DEFAULT_BLOCKSIZE
    block_num: int = 0
    complete: bool = False
    retries: int = 0
    last_packet: bytes = b""
    timer: asyncio.TimerHandle | None = None


@dataclass
class TFTPServerStats:
    """Statistics for a TFTP server session."""
    requests: int = 0
    bytes_sent: int = 0
    transfers_complete: int = 0
    errors: int = 0
    files_complete: list[str] = field(default_factory=list)


class TFTPServerProtocol(asyncio.DatagramProtocol):
    """Async TFTP server protocol.

    Serves files to clients via RRQ. Supports multi-file mode (route by
    requested filename) and single-file mode (serve same data regardless
    of requested filename).
    """

    def __init__(
        self,
        file_data: bytes | None = None,
        filename: str = "firmware.bin",
        on_progress: Callable[[int, int], None] | None = None,
        *,
        files: dict[str, bytes] | None = None,
        done_count: int = 1,
        done_file: str | None = None,
        retransmit_timeout: float = RETRANSMIT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._retransmit_timeout = retransmit_timeout
        self._max_retries = max_retries
        self._files = files or {}
        self._done_file = done_file
        self._default_data = file_data
        self._filename = filename
        self._on_progress = on_progress
        self._transfers: dict[tuple[str, int], TFTPTransfer] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self.stats = TFTPServerStats()
        self._done_count = done_count
        self._done_event = asyncio.Event()
        self._max_blocksize = MAX_BLOCKSIZE

    def set_max_blocksize(self, blocksize: int) -> None:
        """Cap future RFC2348 negotiations without disturbing active transfers."""
        self._max_blocksize = max(8, min(blocksize, MAX_BLOCKSIZE))

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def connection_lost(self, exc: Exception | None) -> None:
        for transfer in self._transfers.values():
            self._cancel_timer(transfer)
        self._transfers.clear()

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

        if addr not in self._transfers and len(self._transfers) >= MAX_TRANSFERS:
            logger.warning(
                "TFTP RRQ from %s:%d ignored: %d transfers already running",
                addr[0], addr[1], len(self._transfers),
            )
            return

        # Resolve file data: try exact match, then basename, then default
        serve_data = self._resolve_file(filename)
        if serve_data is None:
            self._send_error(addr, ERR_FILE_NOT_FOUND, f"File not found: {filename}")
            return

        # Parse options (RFC 2347)
        blocksize = DEFAULT_BLOCKSIZE
        options = {}
        i = 2
        while i < len(parts) - 1:
            opt_name = parts[i].decode("ascii", errors="replace").lower()
            opt_value = parts[i + 1].decode("ascii", errors="replace")
            options[opt_name] = opt_value
            i += 2

        blksize_negotiated = False
        if "blksize" in options:
            try:
                requested_bs = int(options["blksize"])
                blocksize = max(8, min(requested_bs, self._max_blocksize))
                blksize_negotiated = True
                logger.debug(
                    "TFTP blksize requested=%d negotiated=%d",
                    requested_bs,
                    blocksize,
                )
            except ValueError:
                pass

        transfer = TFTPTransfer(
            addr=addr, data=serve_data, filename=filename, blocksize=blocksize,
        )
        old = self._transfers.get(addr)
        if old is not None:
            self._cancel_timer(old)
        self._transfers[addr] = transfer

        # Send OACK whenever the blksize option was accepted, including an
        # explicit fallback to the RFC1350 default of 512 bytes.
        if blksize_negotiated:
            oack = struct.pack("!H", OPCODE_OACK)
            oack += b"blksize\x00" + str(blocksize).encode() + b"\x00"
            self._send_tracked(transfer, oack)
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
            transfer.retries = 0
            self._send_data_block(transfer)
            return

        # Anything else is a duplicate of an ACK already acted on (a held
        # block and its retransmit both arrive): answering it would send
        # every later block twice (RFC 1350's Sorcerer's Apprentice).
        if block_num == transfer.block_num:
            transfer.retries = 0
            self._cancel_timer(transfer)

            if self._on_progress:
                sent = min(transfer.block_num * transfer.blocksize, len(transfer.data))
                self._on_progress(sent, len(transfer.data))

            if transfer.complete:
                self.stats.transfers_complete += 1
                self.stats.files_complete.append(transfer.filename)
                self.stats.bytes_sent += len(transfer.data)
                logger.info("Transfer complete to %s:%d (%d bytes)", addr[0], addr[1], len(transfer.data))
                del self._transfers[addr]
                if self._done_file is not None:
                    if transfer.filename == self._done_file:
                        self._done_event.set()
                elif self.stats.transfers_complete >= self._done_count:
                    self._done_event.set()
                return

            # Send next block
            self._send_data_block(transfer)

    def _resolve_file(self, filename: str) -> bytes | None:
        """Look up file data for a requested filename.

        Tries exact match in files dict, then basename-only match,
        then falls back to default data (single-file mode).
        """
        if self._files:
            if filename in self._files:
                return self._files[filename]
            # Try basename (U-Boot may prepend path)
            import posixpath
            basename = posixpath.basename(filename)
            if basename in self._files:
                return self._files[basename]
            # Try case-insensitive
            for key, data in self._files.items():
                if key.lower() == filename.lower() or key.lower() == basename.lower():
                    return data
        # Fallback: default data (single-file / backward-compat mode)
        return self._default_data

    def _handle_error(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle error from client."""
        self.stats.errors += 1
        if len(data) >= 4:
            error_code = struct.unpack("!H", data[2:4])[0]
            error_msg = data[4:].rstrip(b"\x00").decode("ascii", errors="replace")
            logger.warning("TFTP error from %s:%d: code=%d msg=%s", addr[0], addr[1], error_code, error_msg)

        transfer = self._transfers.pop(addr, None)
        if transfer is not None:
            self._cancel_timer(transfer)

    def _send_data_block(self, transfer: TFTPTransfer) -> None:
        """Send the next data block for a transfer."""
        transfer.block_num += 1
        offset = (transfer.block_num - 1) * transfer.blocksize
        block_data = transfer.data[offset:offset + transfer.blocksize]

        packet = struct.pack("!HH", OPCODE_DATA, transfer.block_num) + block_data
        self._send_tracked(transfer, packet)

        if len(block_data) < transfer.blocksize:
            transfer.complete = True

    def _send_tracked(self, transfer: TFTPTransfer, packet: bytes) -> None:
        """Send a packet the client must ACK, and resend it until it does."""
        transfer.last_packet = packet
        self._send(transfer.addr, packet)
        self._arm_timer(transfer)

    def _arm_timer(self, transfer: TFTPTransfer) -> None:
        self._cancel_timer(transfer)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # driven by hand, with no event loop: nothing to time
        transfer.timer = loop.call_later(
            self._retransmit_timeout, self._retransmit, transfer,
        )

    @staticmethod
    def _cancel_timer(transfer: TFTPTransfer) -> None:
        if transfer.timer is not None:
            transfer.timer.cancel()
            transfer.timer = None

    def _retransmit(self, transfer: TFTPTransfer) -> None:
        transfer.timer = None
        if self._transfers.get(transfer.addr) is not transfer:
            return
        if transfer.retries >= self._max_retries:
            logger.warning(
                "TFTP %s to %s:%d: no ACK for block %d after %d retransmits, "
                "giving up", transfer.filename, transfer.addr[0],
                transfer.addr[1], transfer.block_num, transfer.retries,
            )
            del self._transfers[transfer.addr]
            self.stats.errors += 1
            return
        transfer.retries += 1
        logger.info(
            "TFTP %s to %s:%d: no ACK for block %d in %.1f s, resending (%d/%d)",
            transfer.filename, transfer.addr[0], transfer.addr[1],
            transfer.block_num, self._retransmit_timeout, transfer.retries,
            self._max_retries,
        )
        self._send(transfer.addr, transfer.last_packet)
        self._arm_timer(transfer)

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


def _find_port_holder_cmd(port: int) -> str:
    """The command that names whatever is holding a UDP port, per platform."""
    if sys.platform == "win32":
        return f"netstat -ano -p UDP | findstr :{port}"
    if sys.platform == "darwin":
        return f"sudo lsof -nP -iUDP:{port}"
    return f"sudo ss -ulpn 'sport = :{port}'"


def _add_address_cmd(bind_addr: str) -> str:
    """The command that puts an address on an interface, per platform."""
    if sys.platform == "win32":
        return (
            f'netsh interface ip add address "<adapter>" {bind_addr} '
            f"255.255.255.0"
        )
    if sys.platform == "darwin":
        return f"sudo ifconfig <nic> alias {bind_addr} 255.255.255.0"
    return f"sudo ip addr add {bind_addr}/24 dev <nic>"


def _list_addresses_cmd() -> str:
    """The command that shows what addresses are actually configured."""
    if sys.platform == "win32":
        return "ipconfig"
    if sys.platform == "darwin":
        return "ifconfig -a"
    return "ip -brief address"


def _privileged_port_advice() -> str:
    """How to get a low port, per platform.

    Windows has no reserved-port rule, so a refusal there is a firewall or
    an ACL rather than a missing privilege, and telling the operator to
    "run as root" would send them the wrong way.
    """
    if sys.platform == "win32":
        return (
            "On Windows this is usually a firewall rule or an address "
            "reservation rather than a privilege -- check Windows Defender "
            "Firewall for a UDP block, and `netsh interface ipv4 show "
            "excludedportrange protocol=udp`."
        )
    if sys.platform == "darwin":
        return "Ports below 1024 need root -- run defib with sudo."
    return (
        "Ports below 1024 need root -- run defib with sudo, or grant the "
        "binary CAP_NET_BIND_SERVICE."
    )


def _bind_error_help(bind_addr: str, port: int, exc: OSError) -> str:
    """Turn a bind failure into something the operator can act on."""
    where = f"{bind_addr}:{port}"
    if exc.errno == errno.EADDRINUSE:
        return (
            f"TFTP server cannot bind {where}: address already in use. "
            f"Something else already holds UDP port {port} -- commonly a "
            f"system tftpd or dnsmasq, or an earlier defib run that has not "
            f"exited. Find it with `{_find_port_holder_cmd(port)}` and stop "
            f"it, or use --tftp-via pod if you have a rack pod."
        )
    if exc.errno == errno.EADDRNOTAVAIL:
        return (
            f"TFTP server cannot bind {where}: no interface has that address. "
            f"The host needs {bind_addr} configured on the NIC the camera is "
            f"plugged into, e.g. `{_add_address_cmd(bind_addr)}` -- check "
            f"`{_list_addresses_cmd()}` to see what is actually set."
        )
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"TFTP server cannot bind {where}: permission denied. "
            f"{_privileged_port_advice()}"
        )
    return f"TFTP server cannot bind {where}: {exc}"


async def start_tftp_server(
    file_data: bytes | None = None,
    bind_addr: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    filename: str = "firmware.bin",
    on_progress: Callable[[int, int], None] | None = None,
    *,
    files: dict[str, bytes] | None = None,
    done_count: int = 1,
    done_file: str | None = None,
) -> tuple[asyncio.DatagramTransport, TFTPServerProtocol]:
    """Start an async TFTP server.

    Args:
        file_data: Single firmware data to serve (backward compat).
        bind_addr: Address to bind to.
        port: UDP port (default 69, needs root on Linux).
        filename: Filename to advertise (metadata only).
        on_progress: Callback(bytes_sent, bytes_total) for progress.
        files: Dict mapping filenames to data for multi-file mode.
        done_count: Number of completed transfers before done_event fires.
        done_file: Fire done_event when this file has been sent instead.

    Returns:
        Tuple of (transport, protocol) for the running server.
    """
    loop = asyncio.get_running_loop()
    protocol = TFTPServerProtocol(
        file_data, filename, on_progress,
        files=files, done_count=done_count, done_file=done_file,
    )

    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=(bind_addr, port),
        )
    except OSError as exc:
        raise TFTPBindError(_bind_error_help(bind_addr, port, exc)) from exc

    total = len(file_data) if file_data else sum(len(v) for v in (files or {}).values())
    if files:
        logger.info("TFTP server listening on %s:%d (serving %d files, %d bytes total)",
                     bind_addr, port, len(files), total)
    else:
        logger.info("TFTP server listening on %s:%d (serving %d bytes)", bind_addr, port, total)
    assert isinstance(transport, asyncio.DatagramTransport)
    return transport, protocol
