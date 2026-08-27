"""Tests for the interactive raw terminal bridge."""

import asyncio
import io
import os
import signal

import pytest

from defib.cli.terminal import run_raw_terminal
from defib.transport.base import TransportError
from defib.transport.mock import MockTransport


requires_pty = pytest.mark.skipif(os.name != "posix", reason="PTY tests require POSIX")


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
@requires_pty
async def test_pty_input_is_forwarded_and_transport_output_is_printed() -> None:
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(slave_fd, "rb", buffering=0)
    stdout = io.BytesIO()
    transport = MockTransport()
    transport.enqueue_rx(b"hisilicon # ")

    task = asyncio.create_task(run_raw_terminal(transport, stdin, stdout))
    try:
        await _wait_until(lambda: not termios.tcgetattr(stdin.fileno())[3] & termios.ECHO)
        os.write(master_fd, b"help\r")
        await _wait_until(lambda: b"help\r" in transport.all_tx_data)
        await _wait_until(lambda: b"hisilicon # " in stdout.getvalue())
        os.write(master_fd, b"\x03")
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        os.close(master_fd)
        stdin.close()


@pytest.mark.asyncio
@requires_pty
async def test_sigint_stops_bridge_and_restores_pty() -> None:
    import pty
    import termios

    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(slave_fd, "rb", buffering=0)
    stdout = io.BytesIO()
    transport = MockTransport()
    original_attrs = termios.tcgetattr(stdin.fileno())
    original_handler = signal.getsignal(signal.SIGINT)

    task = asyncio.create_task(run_raw_terminal(transport, stdin, stdout))
    try:
        await _wait_until(lambda: not termios.tcgetattr(stdin.fileno())[3] & termios.ECHO)

        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(task, timeout=1.0)

        assert termios.tcgetattr(stdin.fileno()) == original_attrs
        assert signal.getsignal(signal.SIGINT) == original_handler
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        os.close(master_fd)
        stdin.close()


@pytest.mark.asyncio
async def test_redirected_stdin_is_forwarded_until_eof() -> None:
    stdin = io.BytesIO(b"help\r")
    stdout = io.BytesIO()
    transport = MockTransport()

    await run_raw_terminal(transport, stdin, stdout)

    assert transport.all_tx_data == b"help\r"


@pytest.mark.asyncio
@requires_pty
async def test_transport_disconnect_ends_terminal_cleanly() -> None:
    import pty

    class DisconnectingTransport(MockTransport):
        async def read(self, size: int, timeout: float | None = None) -> bytes:
            raise TransportError("remote disconnected")

    master_fd, slave_fd = pty.openpty()
    stdin = os.fdopen(slave_fd, "rb", buffering=0)
    task = asyncio.create_task(
        run_raw_terminal(DisconnectingTransport(), stdin, io.BytesIO())
    )
    try:
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        os.close(master_fd)
        stdin.close()
