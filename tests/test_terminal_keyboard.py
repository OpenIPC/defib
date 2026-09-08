"""Terminal mode has to carry keystrokes, not just serial output.

OpenIPC/firmware#2381: `defib burn -b -t` put a reporter at a live `OpenIPC #`
prompt on a camera whose flash still had to be rewritten, and the prompt
answered nothing they typed. The loop read the port and wrote the screen; no
code path anywhere in the CLI read stdin. README.md advertised the opposite --
"raw terminal passthrough — type commands directly".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from defib.cli import keyboard
from defib.cli.keyboard import (
    _MAX_BYTES_PER_POLL,
    _read_posix,
    _restore_terminal,
    raw_terminal,
    read_available_keys,
)


class TestNothingTypedIsNothingSent:
    def test_an_idle_keyboard_reads_empty(self, monkeypatch):
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr("select.select", lambda *a, **k: ([], [], []))
        assert read_available_keys() == b""

    def test_a_stdin_without_a_descriptor_is_not_an_error(self, monkeypatch):
        """pytest replaces stdin with an object that has no fileno()."""
        monkeypatch.setattr(keyboard.sys, "platform", "linux")

        class NoFileno:
            def fileno(self):
                raise OSError("captured")

        monkeypatch.setattr(keyboard.sys, "stdin", NoFileno())
        assert read_available_keys() == b""


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="select() takes only sockets on Windows, and this path never runs there",
)
class TestPosixReading:
    """The POSIX reader, on the platforms it actually runs on.

    `read_available_keys` dispatches on the platform, so `_read_posix` is never
    reached on Windows. Forcing it there tests nothing and fails for a reason
    that is not a defect: Windows `select()` accepts sockets only, so the pipe
    these tests use is rejected and the read comes back empty.
    """

    def test_what_was_typed_comes_back(self, monkeypatch):
        r, w = os.pipe()
        os.write(w, b"sf probe 0\n")
        os.close(w)
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr(keyboard.sys, "stdin", _Fd(r))
        try:
            assert _read_posix() == b"sf probe 0\n"
        finally:
            os.close(r)

    def test_eof_on_a_pipe_ends_the_read(self, monkeypatch):
        r, w = os.pipe()
        os.close(w)  # immediate EOF
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr(keyboard.sys, "stdin", _Fd(r))
        try:
            assert _read_posix() == b""
        finally:
            os.close(r)


class TestWindowsReading:
    def test_keys_are_drained_while_the_buffer_has_any(self, monkeypatch):
        monkeypatch.setattr(keyboard.sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(list(b"reset\r")))
        assert read_available_keys() == b"reset\r"

    def test_a_special_key_is_dropped_with_its_scan_code(self, monkeypatch):
        """An arrow key is a marker plus a scan code; U-Boot wants neither."""
        monkeypatch.setattr(keyboard.sys, "platform", "win32")
        keys = [b"a"[0], 0xE0, 0x48, b"b"[0]]  # a, Up, b
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(keys))
        assert read_available_keys() == b"ab"

    def test_the_other_special_prefix_is_dropped_too(self, monkeypatch):
        monkeypatch.setattr(keyboard.sys, "platform", "win32")
        keys = [b"x"[0], 0x00, 0x3B]  # x, F1
        monkeypatch.setitem(sys.modules, "msvcrt", _FakeMsvcrt(keys))
        assert read_available_keys() == b"x"


class TestRawTerminal:
    def test_it_restores_what_it_changed(self, monkeypatch):
        pytest.importorskip("termios")
        import termios

        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        restored = []
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
        monkeypatch.setattr(
            termios, "tcsetattr", lambda fd, when, attrs: restored.append(attrs),
        )
        monkeypatch.setattr("tty.setcbreak", lambda fd: None)
        with raw_terminal(_Fd(0)):
            pass
        assert restored == [["saved"]]

    def test_it_restores_even_when_the_body_raises(self, monkeypatch):
        pytest.importorskip("termios")
        import termios

        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        restored = []
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
        monkeypatch.setattr(
            termios, "tcsetattr", lambda fd, when, attrs: restored.append(attrs),
        )
        monkeypatch.setattr("tty.setcbreak", lambda fd: None)
        with pytest.raises(RuntimeError):
            with raw_terminal(_Fd(0)):
                raise RuntimeError("serial went away")
        assert restored == [["saved"]]

    def test_a_stdin_that_cannot_be_configured_is_not_fatal(self, monkeypatch):
        """Piped or captured stdin still has to run, just without raw mode."""
        monkeypatch.setattr(keyboard.sys, "platform", "linux")

        class NoFileno:
            def fileno(self):
                raise OSError("captured")

        with raw_terminal(NoFileno()):
            pass  # must not raise

    def test_windows_needs_no_termios(self, monkeypatch):
        monkeypatch.setattr(keyboard.sys, "platform", "win32")
        with raw_terminal(None):
            pass


class TestTheDocsMatchTheCode:
    def test_terminal_mode_actually_reads_the_keyboard(self):
        """The regression itself: `-t` promised a console and gave a viewer."""
        # encoding is not optional here: read_text() uses the locale codec,
        # which on Windows is cp1252 and cannot decode this file.
        app = (Path(__file__).parent.parent / "src/defib/cli/app.py").read_text(
            encoding="utf-8",
        )
        block = app.split("Normal U-Boot shell", 1)[1][:2000]
        assert "read_available_keys" in block
        assert "transport.write(typed)" in block

    def test_the_readme_does_not_promise_what_the_code_cannot_do(self):
        readme = (Path(__file__).parent.parent / "README.md").read_text(
            encoding="utf-8",
        )
        assert "type commands directly" not in readme or "keystrokes" in readme


class _Fd:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class _FakeMsvcrt:
    """Just enough msvcrt to drive `_read_windows` off Windows."""

    def __init__(self, keys: list[int]) -> None:
        self._keys = list(keys)

    def kbhit(self) -> bool:
        return bool(self._keys)

    def getch(self) -> bytes:
        return bytes([self._keys.pop(0)])


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="select() takes only sockets on Windows, and this path never runs there",
)
class TestOnePollCannotRunAway:
    """Qodo review on OpenIPC/defib#136.

    The reader drained stdin for as long as it stayed readable. A person types
    a few bytes and it returns at once, but a pipe that keeps producing never
    stops being readable -- so the serial read, the stop flag and the transport
    cleanup all waited for the producer rather than the board.
    """

    def test_a_flood_returns_after_a_bounded_read(self, monkeypatch):
        # The cap is lowered rather than the write enlarged: filling a pipe
        # past its buffer would block this test on the platform with the
        # smallest one, and pipe capacity is not what is under test.
        monkeypatch.setattr(keyboard, "_MAX_BYTES_PER_POLL", 64)
        r, w = os.pipe()
        os.write(w, b"x" * 1024)
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr(keyboard.sys, "stdin", _Fd(r))
        try:
            got = _read_posix()
        finally:
            os.close(w)
            os.close(r)
        assert len(got) == 64

    def test_the_default_cap_is_far_above_human_typing(self):
        assert _MAX_BYTES_PER_POLL >= 1024

    def test_a_short_burst_is_not_truncated(self, monkeypatch):
        """The bound must be invisible to anyone actually typing."""
        r, w = os.pipe()
        os.write(w, b"sf erase 0x0 0x1000000\n")
        os.close(w)
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr(keyboard.sys, "stdin", _Fd(r))
        try:
            assert _read_posix() == b"sf erase 0x0 0x1000000\n"
        finally:
            os.close(r)


class TestABrokenRestoreIsNeverSilent:
    """Qodo review on OpenIPC/defib#136.

    cbreak leaves the shell with no echo and no line editing. If putting it
    back fails and we say nothing, the operator is left with a terminal that
    appears dead and no idea why.
    """

    def test_it_falls_back_to_tcsanow(self, monkeypatch):
        termios = pytest.importorskip("termios")
        attempts = []

        def tcsetattr(fd, when, attrs):
            attempts.append(when)
            if when == termios.TCSADRAIN:
                raise OSError("interrupted")

        monkeypatch.setattr(termios, "tcsetattr", tcsetattr)
        _restore_terminal(0, ["saved"])
        assert attempts == [termios.TCSADRAIN, termios.TCSANOW]

    def test_a_total_failure_tells_the_operator_how_to_recover(
        self, monkeypatch, capsys,
    ):
        termios = pytest.importorskip("termios")

        def always_fails(fd, when, attrs):
            raise OSError("gone")

        monkeypatch.setattr(termios, "tcsetattr", always_fails)
        _restore_terminal(0, ["saved"])
        err = capsys.readouterr().err
        assert "stty sane" in err

    def test_it_does_not_raise_and_so_cannot_mask_the_real_error(
        self, monkeypatch, capsys,
    ):
        termios = pytest.importorskip("termios")
        monkeypatch.setattr(keyboard.sys, "platform", "linux")
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: ["saved"])
        monkeypatch.setattr(
            termios, "tcsetattr",
            lambda fd, when, attrs: (_ for _ in ()).throw(OSError("gone")),
        )
        monkeypatch.setattr("tty.setcbreak", lambda fd: None)

        # The serial failure is what the caller needs to see, not our cleanup.
        with pytest.raises(RuntimeError, match="serial went away"):
            with raw_terminal(_Fd(0)):
                raise RuntimeError("serial went away")
        assert "stty sane" in capsys.readouterr().err
