"""Keystrokes for the serial terminal.

`defib burn -t` advertised a U-Boot console and delivered a viewer: the
terminal-mode loop read the port and wrote the screen, and nothing anywhere
read the keyboard. The reporter in OpenIPC/firmware#2381 reached a live
`OpenIPC #` prompt on a camera whose flash they still had to rewrite, and it
answered none of what they typed.

Both readers here are non-blocking on purpose. They are polled from the same
loop that drains the serial port, so no thread and no executor is involved and
nothing can stall the event loop -- which during a recovery is also holding the
serial link to the board.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator, Protocol

# Windows reports a special key (arrows, function keys, keypad) as a marker
# byte followed by a scan code. Neither means anything to U-Boot, and passing
# them through types garbage at the prompt.
_WINDOWS_SPECIAL_PREFIXES = (b"\x00", b"\xe0")


class _HasFileno(Protocol):
    """Anything a terminal can be configured on -- in practice sys.stdin."""

    def fileno(self) -> int:
        ...


@contextmanager
def raw_terminal(stream: _HasFileno | None = None) -> Iterator[None]:
    """Send keystrokes as they are typed, not a line at a time.

    Also turns off local echo: a serial console echoes back what it received,
    so echoing locally as well shows every character twice.

    Ctrl-C is deliberately left as the interrupt (cbreak keeps ISIG), because
    that is how terminal mode has always been exited and the banner says so.
    A no-op where there is no terminal to configure -- Windows, a pipe, a
    captured stdin under pytest -- so callers need no platform branch.
    """
    stream = stream if stream is not None else sys.stdin
    if sys.platform == "win32":
        yield
        return
    try:
        import termios
        import tty

        fd = stream.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        yield
        return
    try:
        tty.setcbreak(fd)
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass


def read_available_keys() -> bytes:
    """Whatever has been typed since the last call, or b"" if nothing has.

    Never waits. A caller polling this between serial reads keeps typing
    responsive without giving up the loop.
    """
    if sys.platform == "win32":
        return _read_windows()
    return _read_posix()


def _read_windows() -> bytes:
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - only absent off Windows
        return b""

    out = bytearray()
    while msvcrt.kbhit():  # type: ignore[attr-defined]
        char = msvcrt.getch()  # type: ignore[attr-defined]
        if char in _WINDOWS_SPECIAL_PREFIXES:
            msvcrt.getch()  # type: ignore[attr-defined]  # drop the scan code
            continue
        out += char
    return bytes(out)


def _read_posix() -> bytes:
    import select

    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return b""

    out = bytearray()
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 256)
        except OSError:
            break
        if not chunk:  # EOF on a pipe
            break
        out += chunk
    return bytes(out)
