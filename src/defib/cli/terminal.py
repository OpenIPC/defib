"""Bidirectional raw terminal bridge for the burn command."""

from __future__ import annotations

import asyncio
import importlib
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO

from defib.transport.base import Transport, TransportError, TransportTimeout


@contextmanager
def _raw_terminal(stdin: BinaryIO) -> Iterator[None]:
    """Disable canonical input, translations, and local echo."""
    if os.name != "posix" or not stdin.isatty():
        yield
        return

    import termios
    import tty

    fd = stdin.fileno()
    previous = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


async def _pump_posix_stdin(
    transport: Transport,
    fd: int,
    stop: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable() -> None:
        try:
            data = os.read(fd, 1024)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
        queue.put_nowait(data)

    loop.add_reader(fd, on_readable)
    try:
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if not data:
                stop.set()
                return
            if not await _forward_input(transport, data, stop):
                return
    finally:
        loop.remove_reader(fd)


async def _forward_input(
    transport: Transport,
    data: bytes,
    stop: asyncio.Event,
) -> bool:
    """Forward input bytes, treating Ctrl-C as a local terminal command."""
    before_sigint, separator, _ = data.partition(b"\x03")
    if before_sigint:
        await transport.write(before_sigint)
    if separator:
        stop.set()
        return False
    return True


async def _pump_stream_stdin(
    transport: Transport,
    stdin: BinaryIO,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        data = await asyncio.to_thread(stdin.read, 1024)
        if not data:
            stop.set()
            return
        if not await _forward_input(transport, data, stop):
            return


async def _pump_windows_console(
    transport: Transport,
    stop: asyncio.Event,
) -> None:
    msvcrt = importlib.import_module("msvcrt")

    while not stop.is_set():
        if not msvcrt.kbhit():
            await asyncio.sleep(0.01)
            continue
        char = msvcrt.getch()
        if char in (b"\x00", b"\xe0"):
            msvcrt.getch()
            continue
        if not await _forward_input(transport, char, stop):
            return


async def _pump_transport(
    transport: Transport,
    stdout: BinaryIO,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            data = await transport.read(256, timeout=0.1)
        except TransportTimeout:
            continue
        if not data:
            stop.set()
            return
        stdout.write(data)
        stdout.flush()


async def _bridge_terminal(
    transport: Transport,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stop: asyncio.Event,
) -> None:
    if os.name == "nt" and stdin.isatty():
        stdin_task = asyncio.create_task(_pump_windows_console(transport, stop))
    elif stdin.isatty():
        stdin_task = asyncio.create_task(_pump_posix_stdin(transport, stdin.fileno(), stop))
    else:
        stdin_task = asyncio.create_task(_pump_stream_stdin(transport, stdin, stop))
    output_task = asyncio.create_task(_pump_transport(transport, stdout, stop))
    stop_task = asyncio.create_task(stop.wait())
    tasks = {stdin_task, output_task, stop_task}

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        if task is not stop_task:
            task.result()


async def run_raw_terminal(
    transport: Transport,
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> None:
    """Bridge stdin and transport until EOF, disconnect, or Ctrl-C."""
    stop = asyncio.Event()
    previous_handler = signal.getsignal(signal.SIGINT)

    def on_sigint(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        with _raw_terminal(stdin):
            try:
                await _bridge_terminal(transport, stdin, stdout, stop)
            except TransportError:
                pass
    finally:
        signal.signal(signal.SIGINT, previous_handler)
