"""Tests for the RFC 2217 (Telnet COM Port Control) transport.

Covers:

- ``rfc2217://`` URL-scheme dispatch through ``create_transport``.
- Wrapper-level behaviour with a mocked pyserial port (overflow buffer,
  timeout enforcement, modem-control delegation, flush, close).
- End-to-end protocol exchange against a small in-process RFC 2217
  server fixture (negotiation, ``SET-CONTROL`` / ``SET-BAUDRATE``
  sub-options, IAC-escaped binary data round-trip).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from unittest.mock import MagicMock

import pytest
import serial

from defib.transport.base import TransportTimeout
from defib.transport.rfc2217 import Rfc2217Transport


# ===========================================================================
# Fake RFC 2217 server fixture
# ===========================================================================

# Telnet
_IAC, _DO, _DONT, _WILL, _WONT = 0xff, 0xfd, 0xfe, 0xfb, 0xfc
_SB, _SE = 0xfa, 0xf0
_OPT_BINARY, _OPT_SGA, _OPT_COMPORT = 0, 3, 44

# COM-PORT-OPTION sub-options (client side)
_SIGNATURE = 0
_SET_BAUDRATE = 1
_SET_DATASIZE = 2
_SET_PARITY = 3
_SET_STOPSIZE = 4
_SET_CONTROL = 5
_PURGE_DATA = 12
_SERVER_OFFSET = 100  # server replies use sub-option + 100


class FakeRfc2217Server:
    """Minimal RFC 2217 server: just enough for pyserial's ``open()``
    to succeed and for tests to verify which sub-options the client
    issues.  Mirrors the upstream Vectis server behaviour for the
    sub-options the defib transport exercises.
    """

    def __init__(self) -> None:
        self.set_control_history: list[int] = []
        self.set_baudrate_history: list[int] = []
        self.set_datasize_history: list[int] = []
        self.set_parity_history: list[int] = []
        self.set_stopsize_history: list[int] = []
        self.purge_history: list[int] = []
        self.received_data = bytearray()
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle_client, host="127.0.0.1", port=0,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def send_uart_bytes(self, data: bytes) -> None:
        """Inject bytes from the simulated UART side toward the client.
        IAC bytes are escaped per RFC 854."""
        if self._writer is None:
            return
        self._writer.write(data.replace(b"\xff", b"\xff\xff"))
        await self._writer.drain()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._writer = writer
        try:
            # Proactively negotiate BINARY + COM-PORT-OPTION (and SGA).
            writer.write(bytes([
                _IAC, _WILL, _OPT_BINARY,
                _IAC, _DO,   _OPT_BINARY,
                _IAC, _WILL, _OPT_SGA,
                _IAC, _DO,   _OPT_SGA,
                _IAC, _WILL, _OPT_COMPORT,
                _IAC, _DO,   _OPT_COMPORT,
            ]))
            await writer.drain()
            await self._consume(reader, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _consume(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        state = "data"
        sb_opt = 0
        sb_buf = bytearray()

        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            for b in chunk:
                if state == "data":
                    if b == _IAC:
                        state = "iac"
                    else:
                        self.received_data.append(b)
                elif state == "iac":
                    if b == _IAC:
                        self.received_data.append(b)
                        state = "data"
                    elif b in (_WILL, _WONT, _DO, _DONT):
                        state = "neg"  # ack-by-ignoring is fine for tests
                    elif b == _SB:
                        state = "sb_opt"
                    else:
                        state = "data"
                elif state == "neg":
                    state = "data"
                elif state == "sb_opt":
                    sb_opt = b
                    sb_buf = bytearray()
                    state = "sb_data"
                elif state == "sb_data":
                    if b == _IAC:
                        state = "sb_iac"
                    else:
                        sb_buf.append(b)
                elif state == "sb_iac":
                    if b == _SE:
                        self._dispatch(sb_opt, bytes(sb_buf), writer)
                        state = "data"
                    elif b == _IAC:
                        sb_buf.append(_IAC)
                        state = "sb_data"
                    else:
                        state = "data"
            await writer.drain()

    def _dispatch(
        self,
        opt: int,
        data: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        if opt != _OPT_COMPORT or not data:
            return
        sub = data[0]
        if sub == _SET_CONTROL and len(data) >= 2:
            value = data[1]
            self.set_control_history.append(value)
            self._reply_byte(writer, _SET_CONTROL, value)
        elif sub == _SET_BAUDRATE and len(data) >= 5:
            baud = int.from_bytes(data[1:5], "big")
            self.set_baudrate_history.append(baud)
            writer.write(bytes([
                _IAC, _SB, _OPT_COMPORT, _SET_BAUDRATE + _SERVER_OFFSET,
                *baud.to_bytes(4, "big"),
                _IAC, _SE,
            ]))
        elif sub == _SET_DATASIZE and len(data) >= 2:
            self.set_datasize_history.append(data[1])
            self._reply_byte(writer, _SET_DATASIZE, 8)  # we always serve 8N1
        elif sub == _SET_PARITY and len(data) >= 2:
            self.set_parity_history.append(data[1])
            self._reply_byte(writer, _SET_PARITY, 1)  # NONE
        elif sub == _SET_STOPSIZE and len(data) >= 2:
            self.set_stopsize_history.append(data[1])
            self._reply_byte(writer, _SET_STOPSIZE, 1)
        elif sub == _PURGE_DATA and len(data) >= 2:
            self.purge_history.append(data[1])
            self._reply_byte(writer, _PURGE_DATA, data[1])
        elif sub == _SIGNATURE:
            # Empty signature
            writer.write(bytes([
                _IAC, _SB, _OPT_COMPORT, _SIGNATURE + _SERVER_OFFSET,
                _IAC, _SE,
            ]))

    @staticmethod
    def _reply_byte(
        writer: asyncio.StreamWriter, sub: int, value: int,
    ) -> None:
        writer.write(bytes([
            _IAC, _SB, _OPT_COMPORT, sub + _SERVER_OFFSET, value,
            _IAC, _SE,
        ]))


@pytest.fixture
async def fake_rfc2217() -> Any:
    """Yield a FakeRfc2217Server bound to an ephemeral port."""
    srv = FakeRfc2217Server()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


# ===========================================================================
# URL-scheme dispatch
# ===========================================================================

class TestUrlScheme:
    async def test_rfc2217_dispatched_via_create_transport(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from defib.transport import serial_platform

        captured: dict[str, Any] = {}

        async def fake_create(url: str, baudrate: int = 115200) -> Any:
            captured["url"] = url
            captured["baudrate"] = baudrate
            return MagicMock()

        monkeypatch.setattr(Rfc2217Transport, "create", fake_create)
        await serial_platform.create_transport(
            "rfc2217://192.0.2.1:35240", baudrate=115200,
        )
        assert captured["url"] == "rfc2217://192.0.2.1:35240"
        assert captured["baudrate"] == 115200

    async def test_baudrate_threaded_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from defib.transport import serial_platform

        captured: dict[str, Any] = {}

        async def fake_create(url: str, baudrate: int = 115200) -> Any:
            captured["baudrate"] = baudrate
            return MagicMock()

        monkeypatch.setattr(Rfc2217Transport, "create", fake_create)
        await serial_platform.create_transport(
            "rfc2217://localhost:35200", baudrate=921600,
        )
        assert captured["baudrate"] == 921600


# ===========================================================================
# Wrapper-level behaviour (mocked pyserial port)
# ===========================================================================

class TestOverflowBuffer:
    """pyserial's RFC 2217 ``read(size)`` returns a whole queue chunk
    when ``len(chunk) >= size``, possibly more bytes than asked.  Our
    transport must cap the return at ``size`` and keep the overflow."""

    async def test_overflow_stashed_for_next_read(self) -> None:
        port = MagicMock()
        port.read.side_effect = [b"hello", b"world"]  # 5-byte chunks
        port.is_open = True

        t = Rfc2217Transport(port)

        first = await t.read(1)
        assert first == b"h"

        # Local buffer now has b"ello"; second read should drain it
        # without calling pyserial again.
        second = await t.read(4)
        assert second == b"ello"
        assert port.read.call_count == 1

        # Buffer empty; next read pulls a fresh chunk from pyserial.
        third = await t.read(5)
        assert third == b"world"

    async def test_exact_size_no_overflow(self) -> None:
        port = MagicMock()
        port.read.return_value = b"abc"
        port.is_open = True
        t = Rfc2217Transport(port)
        result = await t.read(3)
        assert result == b"abc"
        assert t._buf == bytearray()

    async def test_unread_prepends_to_buffer(self) -> None:
        port = MagicMock()
        port.is_open = True
        # Stash some bytes via read overflow first.
        calls = {"n": 0}
        def fake_read(_size: int) -> bytes:
            calls["n"] += 1
            return b"XYZab" if calls["n"] == 1 else b""
        port.read.side_effect = fake_read

        t = Rfc2217Transport(port)
        first = await t.read(3)
        assert first == b"XYZ"
        # _buf has b"ab".  Now unread b"!!".
        await t.unread(b"!!")
        # Next read should see "!!" first, then "ab".
        result = await t.read(4)
        assert result == b"!!ab"


class TestTimeout:
    async def test_pyserial_timeout_not_mutated_per_read(self) -> None:
        """Critical: setting pyserial-rfc2217's .timeout triggers a full
        port re-renegotiation (~400 ms / call on a real link).  Our
        wrapper must NOT touch it after open."""
        port = MagicMock()
        port.read.return_value = b""
        port.is_open = True
        port.timeout = 0.01  # whatever was set at open

        t = Rfc2217Transport(port)
        with pytest.raises(TransportTimeout):
            await t.read(10, timeout=0.05)

        assert port.timeout == 0.01  # untouched

    async def test_returns_partial_on_timeout(self) -> None:
        """If timeout expires after some bytes were collected, return
        the partial result rather than raising."""
        port = MagicMock()
        # First call returns 2 bytes, subsequent calls return empty
        # (pyserial's quantum timeout).  Use a side_effect function so
        # we don't run out of pre-canned responses.
        calls = {"n": 0}
        def fake_read(_size: int) -> bytes:
            calls["n"] += 1
            return b"hi" if calls["n"] == 1 else b""
        port.read.side_effect = fake_read
        port.is_open = True

        t = Rfc2217Transport(port)
        result = await t.read(10, timeout=0.1)
        assert result == b"hi"

    async def test_raises_on_timeout_with_no_bytes(self) -> None:
        port = MagicMock()
        port.read.return_value = b""
        port.is_open = True
        t = Rfc2217Transport(port)
        with pytest.raises(TransportTimeout):
            await t.read(5, timeout=0.05)


class TestModemControlDelegation:
    """``set_dtr`` / ``set_rts`` / ``set_baudrate`` must update pyserial
    properties — the actual RFC 2217 sub-option emission is pyserial's
    job (and is exercised in the TestE2E section below)."""

    async def test_set_dtr_updates_pyserial_attr(self) -> None:
        port = MagicMock()
        port.dtr = True
        t = Rfc2217Transport(port)
        await t.set_dtr(False)
        assert port.dtr is False
        await t.set_dtr(True)
        assert port.dtr is True

    async def test_set_rts_updates_pyserial_attr(self) -> None:
        port = MagicMock()
        port.rts = True
        t = Rfc2217Transport(port)
        await t.set_rts(False)
        assert port.rts is False
        await t.set_rts(True)
        assert port.rts is True

    async def test_set_baudrate_updates_pyserial_attr(self) -> None:
        port = MagicMock()
        port.baudrate = 9600
        t = Rfc2217Transport(port)
        await t.set_baudrate(115200)
        assert port.baudrate == 115200


class TestWriteFlushClose:
    async def test_write_delegates(self) -> None:
        port = MagicMock()
        t = Rfc2217Transport(port)
        await t.write(b"\xfe\x10\x00\xff")
        port.write.assert_called_once_with(b"\xfe\x10\x00\xff")

    async def test_flush_input_clears_local_and_pyserial(self) -> None:
        port = MagicMock()
        t = Rfc2217Transport(port)
        t._buf = bytearray(b"stale")
        await t.flush_input()
        assert t._buf == bytearray()
        port.reset_input_buffer.assert_called_once()

    async def test_bytes_waiting_includes_local_buffer(self) -> None:
        port = MagicMock()
        port.in_waiting = 7
        t = Rfc2217Transport(port)
        t._buf = bytearray(b"abc")
        assert await t.bytes_waiting() == 10  # 3 local + 7 in pyserial

    async def test_close_when_open(self) -> None:
        port = MagicMock()
        port.is_open = True
        t = Rfc2217Transport(port)
        await t.close()
        port.close.assert_called_once()

    async def test_close_when_already_closed(self) -> None:
        port = MagicMock()
        port.is_open = False
        t = Rfc2217Transport(port)
        await t.close()
        port.close.assert_not_called()


# ===========================================================================
# End-to-end protocol exchange against the fake server
# ===========================================================================

class TestE2EAgainstFakeServer:
    """Open a real pyserial RFC 2217 client against the fake server,
    issue calls through our wrapper, and verify the wire-level
    protocol behaviour."""

    async def test_open_and_close(self, fake_rfc2217: FakeRfc2217Server) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        await t.close()

    async def test_negotiation_sends_default_port_settings(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        """pyserial sends SET-BAUDRATE + SET-DATASIZE + SET-PARITY +
        SET-STOPSIZE during open; verify the server saw them."""
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            assert 115200 in fake_rfc2217.set_baudrate_history
            assert 8 in fake_rfc2217.set_datasize_history
            assert 1 in fake_rfc2217.set_parity_history
            assert 1 in fake_rfc2217.set_stopsize_history
        finally:
            await t.close()

    async def test_set_dtr_off_sends_value_9(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            fake_rfc2217.set_control_history.clear()
            await t.set_dtr(False)
            await asyncio.sleep(0.05)
            assert 9 in fake_rfc2217.set_control_history
        finally:
            await t.close()

    async def test_set_dtr_on_sends_value_8(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            fake_rfc2217.set_control_history.clear()
            await t.set_dtr(True)
            await asyncio.sleep(0.05)
            assert 8 in fake_rfc2217.set_control_history
        finally:
            await t.close()

    async def test_set_rts_off_sends_value_12(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            fake_rfc2217.set_control_history.clear()
            await t.set_rts(False)
            await asyncio.sleep(0.05)
            assert 12 in fake_rfc2217.set_control_history
        finally:
            await t.close()

    async def test_set_rts_on_sends_value_11(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            fake_rfc2217.set_control_history.clear()
            await t.set_rts(True)
            await asyncio.sleep(0.05)
            assert 11 in fake_rfc2217.set_control_history
        finally:
            await t.close()

    async def test_set_baudrate_sub_option(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            fake_rfc2217.set_baudrate_history.clear()
            await t.set_baudrate(921600)
            await asyncio.sleep(0.05)
            assert 921600 in fake_rfc2217.set_baudrate_history
        finally:
            await t.close()

    async def test_data_round_trip_with_iac_escape(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        """Bytes containing ``0xFF`` must round-trip in both directions
        with proper IAC IAC escaping handled by pyserial + our server."""
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            # Client → server: every byte 0x00..0xFF, including 0x10 + 0xFF.
            payload = bytes(range(256))
            fake_rfc2217.received_data.clear()
            await t.write(payload)
            await asyncio.sleep(0.1)
            assert bytes(fake_rfc2217.received_data) == payload

            # Server → client: same payload coming back through transport.
            await fake_rfc2217.send_uart_bytes(payload)
            received = b""
            deadline = asyncio.get_event_loop().time() + 1.0
            while len(received) < len(payload) and asyncio.get_event_loop().time() < deadline:
                try:
                    chunk = await t.read(len(payload) - len(received), timeout=0.1)
                    received += chunk
                except TransportTimeout:
                    pass
            assert received == payload
        finally:
            await t.close()

    async def test_tcp_nodelay_set_on_socket(
        self, fake_rfc2217: FakeRfc2217Server,
    ) -> None:
        """Tight handshake loops over a high-RTT link need TCP_NODELAY
        — without it, Nagle batches our 0xAA flood out of the bootrom's
        catch window."""
        t = await Rfc2217Transport.create(
            f"rfc2217://127.0.0.1:{fake_rfc2217.port}", baudrate=115200,
        )
        try:
            sock = t._port._socket
            assert sock is not None
            opt = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            assert opt == 1, "TCP_NODELAY must be enabled on the RFC 2217 socket"
        finally:
            await t.close()


# ===========================================================================
# create() error handling
# ===========================================================================

class TestCreateErrors:
    async def test_unreachable_url_raises_transport_error(self) -> None:
        # Pick a port that nothing should be listening on
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            unused = s.getsockname()[1]
        # s is now closed; the port is briefly free, nothing listening.
        from defib.transport.base import TransportError
        with pytest.raises((TransportError, serial.SerialException)):
            await Rfc2217Transport.create(
                f"rfc2217://127.0.0.1:{unused}", baudrate=115200,
            )
