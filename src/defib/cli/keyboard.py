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

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator, Protocol

logger = logging.getLogger(__name__)

# Windows reports a special key (arrows, function keys, keypad) as a marker
# byte followed by a scan code. Neither means anything to U-Boot, and passing
# them through types garbage at the prompt.
_WINDOWS_SPECIAL_PREFIXES = (b"\x00", b"\xe0")

# Most one poll may collect before handing the loop back. A person types a
# few bytes; a pipe never stops, and draining it until it pauses would keep
# the serial read and the stop flag waiting for as long as the producer runs.
_MAX_BYTES_PER_POLL = 4096


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
        _restore_terminal(fd, saved)


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
    while len(out) < _MAX_BYTES_PER_POLL and msvcrt.kbhit():  # type: ignore[attr-defined]
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
    while len(out) < _MAX_BYTES_PER_POLL:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, min(256, _MAX_BYTES_PER_POLL - len(out)))
        except OSError:
            break
        if not chunk:  # EOF on a pipe
            break
        out += chunk
    return bytes(out)


def _restore_terminal(fd: int, saved: Any) -> None:
    """Put the terminal back the way it was, and say so if that fails.

    This is the only thing between a failed restore and a shell with no echo
    and no line editing. Discarding the failure -- which is what this used to
    do -- leaves the operator with a broken terminal and nothing to explain it.

    TCSADRAIN waits for pending output and so can be the half that fails; it is
    the right first choice because it does not truncate what the board was
    printing, and TCSANOW is worth trying before giving up.

    Never raises. The body may already be unwinding with the error that
    actually matters, and a cleanup failure must not replace it.
    """
    import termios

    for when in (termios.TCSADRAIN, termios.TCSANOW):
        try:
            termios.tcsetattr(fd, when, saved)
            return
        except Exception as exc:  # noqa: PERF203 - the retry is the point
            logger.debug("tcsetattr(%s) failed: %s", when, exc)

    # stderr rather than logging alone: this has to reach someone whose
    # terminal has just stopped echoing, whatever their logging setup is.
    print(
        "defib could not restore your terminal settings. "
        "Run `stty sane` (you may have to type it blind) to get echo back.",
        file=sys.stderr,
    )
