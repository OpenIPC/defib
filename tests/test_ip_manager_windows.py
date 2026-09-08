"""Temporary-IP management must not fight the host it is running on.

OpenIPC/firmware#2381: a reporter recovering a bricked Hi3516CV300 on Windows
got `defib install` as far as "Phase 2: Flash via TFTP" and no further. Three
separate faults, all in this module:

  * the adapter name came from ``socket.if_nameindex()`` ("ethernet_0"), which
    netsh does not accept -- it answered "Failed to configure the DHCP service.
    The interface may be disconnected." and they went looking for a cable;
  * assigning an address that was already there was fatal ("The object already
    exists"), so setting it up by hand first did not help either;
  * netsh returns before the address can be bound, so the TFTP bind that came
    next raced it, lost, and unwound the context manager -- which removed the
    address, making the log read as though defib had taken away what it had
    just assigned.

These tests pin the three fixes and the promise that we only tear down what we
put up.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from defib.network import ip_manager
from defib.network.ip_manager import (
    IPManagerError,
    _parse_netsh_interfaces,
    _windows_interfaces,
    add_ip,
    list_interfaces,
    list_interfaces_async,
    temporary_ip,
)


NETSH_SHOW_INTERFACE = """
Admin State    State          Type             Interface Name
-------------------------------------------------------------------------
Enabled        Connected      Dedicated        Ethernet
Enabled        Disconnected   Dedicated        Wi-Fi
Enabled        Connected      Dedicated        Ethernet 2
"""


class TestWindowsInterfaceNames:
    def test_names_come_from_netsh_not_if_nameindex(self, monkeypatch):
        """The names must be the ones netsh will take back."""
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, NETSH_SHOW_INTERFACE, "",
            ),
        )
        assert _windows_interfaces() == ["Ethernet", "Wi-Fi", "Ethernet 2"]

    def test_a_name_with_a_space_survives(self, monkeypatch):
        """"Ethernet 2" is a real default name; splitting on whitespace loses it."""
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, NETSH_SHOW_INTERFACE, "",
            ),
        )
        assert "Ethernet 2" in _windows_interfaces()

    def test_the_header_is_not_an_adapter(self, monkeypatch):
        """netsh is localised, so the header is skipped by position, not by text."""
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, NETSH_SHOW_INTERFACE, "",
            ),
        )
        assert "Interface Name" not in _windows_interfaces()

    def test_no_netsh_falls_back_rather_than_raising(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("netsh")),
        )
        assert _windows_interfaces() == []

    def test_windows_does_not_go_through_if_nameindex(self, monkeypatch):
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")
        monkeypatch.setattr(ip_manager, "_windows_interfaces", lambda: ["Ethernet"])
        assert list_interfaces() == ["Ethernet"]

    @pytest.mark.skipif(
        not sys.platform.startswith("win"), reason="needs a real netsh",
    )
    def test_real_netsh_yields_at_least_one_adapter(self):
        """The mocks above pin the parser; this pins it against the real thing.

        CI runs this matrix on windows-latest, so the one claim the fix rests
        on -- that netsh can be asked for names netsh will accept -- is checked
        on a real Windows host rather than only against a captured table.
        """
        assert _windows_interfaces(), (
            "netsh listed no adapters; the parser or the command has drifted"
        )


class TestAddressAlreadyThere:
    @pytest.mark.asyncio
    async def test_an_address_already_up_is_success_not_failure(self, monkeypatch):
        """"The object already exists" is the state we wanted."""
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: True)

        async def fail(cmd):
            raise AssertionError("should not have run a command")

        monkeypatch.setattr(ip_manager, "_run_command", fail)
        assert await add_ip("Ethernet", "192.168.1.10") is False

    @pytest.mark.asyncio
    async def test_a_command_that_fails_but_leaves_it_usable_is_success(self, monkeypatch):
        """Exit status is not the authority; bindability is."""
        states = iter([False, True])
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: next(states))

        async def already_exists(cmd):
            return 1, "", "The object already exists."

        monkeypatch.setattr(ip_manager, "_run_command", already_exists)
        assert await add_ip("Ethernet", "192.168.1.10") is False

    @pytest.mark.asyncio
    async def test_a_genuine_failure_still_raises_and_names_the_adapters(self, monkeypatch):
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")
        monkeypatch.setattr(ip_manager, "_windows_interfaces", lambda: ["Ethernet"])

        async def disconnected(cmd):
            return 1, "", "The interface may be disconnected."

        monkeypatch.setattr(ip_manager, "_run_command", disconnected)
        with pytest.raises(IPManagerError) as exc:
            await add_ip("ethernet_0", "192.168.1.10")
        # The message has to point at the name mismatch, which is the cause.
        assert "friendly name" in str(exc.value)
        assert "Ethernet" in str(exc.value)
        assert "--nic" in str(exc.value)


class TestBindRace:
    @pytest.mark.asyncio
    async def test_add_waits_for_the_address_to_become_usable(self, monkeypatch):
        """netsh returns early; the caller binds immediately. Wait it out."""
        calls = {"n": 0}

        def bindable(ip):
            calls["n"] += 1
            return calls["n"] > 3   # not there, not there, not there, there

        monkeypatch.setattr(ip_manager, "_bindable", bindable)

        async def ok(cmd):
            return 0, "", ""

        monkeypatch.setattr(ip_manager, "_run_command", ok)
        assert await add_ip("Ethernet", "192.168.1.10") is True
        assert calls["n"] > 1, "did not poll at all"

    @pytest.mark.asyncio
    async def test_an_address_that_never_comes_up_says_so(self, monkeypatch):
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)

        async def ok(cmd):
            return 0, "", ""

        monkeypatch.setattr(ip_manager, "_run_command", ok)

        async def instant(ip, timeout=10.0):
            return False

        monkeypatch.setattr(ip_manager, "_wait_until_bindable", instant)
        with pytest.raises(IPManagerError, match="never became usable"):
            await add_ip("Ethernet", "192.168.1.10")


class TestTeardownOnlyUndoesWhatWeDid:
    @pytest.mark.asyncio
    async def test_an_address_we_added_is_removed(self, monkeypatch):
        removed = []
        monkeypatch.setattr(ip_manager, "add_ip", _returning(True))
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))
        async with temporary_ip("Ethernet", "192.168.1.10"):
            pass
        assert removed == [("Ethernet", "192.168.1.10")]

    @pytest.mark.asyncio
    async def test_an_address_that_was_already_there_is_left_alone(self, monkeypatch):
        """Taking away the operator's own static IP is not ours to do."""
        removed = []
        monkeypatch.setattr(ip_manager, "add_ip", _returning(False))
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))
        async with temporary_ip("Ethernet", "192.168.1.10"):
            pass
        assert removed == []

    @pytest.mark.asyncio
    async def test_a_failure_inside_the_block_still_removes_ours(self, monkeypatch):
        removed = []
        monkeypatch.setattr(ip_manager, "add_ip", _returning(True))
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))
        with pytest.raises(RuntimeError):
            async with temporary_ip("Ethernet", "192.168.1.10"):
                raise RuntimeError("TFTP bind failed")
        assert removed == [("Ethernet", "192.168.1.10")]


def _returning(value):
    async def _add(interface, ip, netmask="255.255.255.0"):
        return value
    return _add


def _recording(sink):
    async def _remove(interface, ip, netmask="255.255.255.0"):
        sink.append((interface, ip))
    return _remove


class TestTheAddressIsNeverStranded:
    """Qodo review on OpenIPC/defib#135.

    `add_ip` returns ownership to `temporary_ip`, so anything that raises
    between assigning the address and returning happens while nobody is in a
    position to clean up. A stranded address is not just litter: the next run
    sees it already up and reads it as the operator's, so it is never removed.
    """

    @pytest.mark.asyncio
    async def test_a_wait_that_times_out_takes_the_address_back_down(self, monkeypatch):
        removed = []
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)

        async def ok(cmd):
            return 0, "", ""

        async def never(ip, timeout=10.0):
            return False

        monkeypatch.setattr(ip_manager, "_run_command", ok)
        monkeypatch.setattr(ip_manager, "_wait_until_bindable", never)
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))

        with pytest.raises(IPManagerError, match="never became usable"):
            await add_ip("Ethernet", "192.168.1.10")
        assert removed == [("Ethernet", "192.168.1.10")]

    @pytest.mark.asyncio
    async def test_a_cancelled_wait_takes_the_address_back_down(self, monkeypatch):
        """Ctrl-C during a recovery must not leave the host reconfigured."""
        removed = []
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)

        async def ok(cmd):
            return 0, "", ""

        async def cancelled(ip, timeout=10.0):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ip_manager, "_run_command", ok)
        monkeypatch.setattr(ip_manager, "_wait_until_bindable", cancelled)
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))

        with pytest.raises(asyncio.CancelledError):
            await add_ip("Ethernet", "192.168.1.10")
        assert removed == [("Ethernet", "192.168.1.10")]

    @pytest.mark.asyncio
    async def test_a_rollback_that_fails_does_not_hide_the_real_error(self, monkeypatch):
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)

        async def ok(cmd):
            return 0, "", ""

        async def never(ip, timeout=10.0):
            return False

        async def blows_up(interface, ip, netmask="255.255.255.0"):
            raise OSError("netsh went away")

        monkeypatch.setattr(ip_manager, "_run_command", ok)
        monkeypatch.setattr(ip_manager, "_wait_until_bindable", never)
        monkeypatch.setattr(ip_manager, "remove_ip", blows_up)

        with pytest.raises(IPManagerError, match="never became usable"):
            await add_ip("Ethernet", "192.168.1.10")

    @pytest.mark.asyncio
    async def test_an_address_that_was_already_there_is_not_rolled_back(self, monkeypatch):
        removed = []
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: True)
        monkeypatch.setattr(ip_manager, "remove_ip", _recording(removed))
        assert await add_ip("Ethernet", "192.168.1.10") is False
        assert removed == []


class TestNetshStaysOffTheEventLoop:
    """Qodo review on OpenIPC/defib#135.

    A synchronous `subprocess.run` reached from a coroutine stops every other
    task until it returns -- during a recovery that includes the serial link to
    a camera sitting in its bootrom window, which does not wait for us.
    """

    @pytest.mark.asyncio
    async def test_the_async_path_does_not_call_subprocess_run(self, monkeypatch):
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")

        def forbidden(*a, **k):
            raise AssertionError("blocking subprocess.run on the event loop")

        monkeypatch.setattr(subprocess, "run", forbidden)

        async def fake(cmd):
            return 0, NETSH_SHOW_INTERFACE, ""

        monkeypatch.setattr(ip_manager, "_run_command", fake)
        assert await list_interfaces_async() == ["Ethernet", "Wi-Fi", "Ethernet 2"]

    @pytest.mark.asyncio
    async def test_a_netsh_that_hangs_does_not_hang_the_recovery(self, monkeypatch):
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")
        monkeypatch.setattr(ip_manager, "_NETSH_TIMEOUT", 0.05)

        async def hangs(cmd):
            await asyncio.sleep(30)
            return 0, "", ""

        monkeypatch.setattr(ip_manager, "_run_command", hangs)
        # Falls back rather than stalling, and does so promptly.
        assert await list_interfaces_async() == ["Ethernet", "Wi-Fi"]

    @pytest.mark.asyncio
    async def test_a_failing_netsh_falls_back(self, monkeypatch):
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")

        async def fails(cmd):
            return 1, "", "The requested operation requires elevation."

        monkeypatch.setattr(ip_manager, "_run_command", fails)
        assert await list_interfaces_async() == ["Ethernet", "Wi-Fi"]

    @pytest.mark.asyncio
    async def test_the_failure_advice_is_async_too(self, monkeypatch):
        """add_ip's own error message reaches the enumerator; that path counts."""
        monkeypatch.setattr(ip_manager.sys, "platform", "win32")
        monkeypatch.setattr(ip_manager, "_bindable", lambda ip: False)

        def forbidden(*a, **k):
            raise AssertionError("blocking subprocess.run on the event loop")

        monkeypatch.setattr(subprocess, "run", forbidden)

        async def dispatch(cmd):
            if cmd[:2] == ["netsh", "interface"] and "show" in cmd:
                return 0, NETSH_SHOW_INTERFACE, ""
            return 1, "", "The interface may be disconnected."

        monkeypatch.setattr(ip_manager, "_run_command", dispatch)
        with pytest.raises(IPManagerError) as exc:
            await add_ip("ethernet_0", "192.168.1.10")
        assert "Ethernet" in str(exc.value)


class TestTheParserIsSeparableFromTheCommand:
    def test_parsing_needs_no_subprocess_at_all(self):
        """Splitting the two is what let the async path reuse the parser."""
        assert _parse_netsh_interfaces(NETSH_SHOW_INTERFACE) == [
            "Ethernet", "Wi-Fi", "Ethernet 2",
        ]

    def test_an_empty_table_is_not_an_adapter(self):
        assert _parse_netsh_interfaces("") == []
