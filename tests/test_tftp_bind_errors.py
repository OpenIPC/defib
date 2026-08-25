"""TFTP bind failures must say what to do about them.

OpenIPC/firmware#2299: a user running `defib install` got "OS Address
already in use" in the middle of a flash recovery. The kernel's text names
neither the address nor the fix, and at that point in a recovery guessing is
expensive. These tests pin the actionable form.
"""
import asyncio
import errno
import socket

import pytest

from defib.network.tftp_server import (
    TFTPBindError,
    _add_address_cmd,
    _bind_error_help,
    _find_port_holder_cmd,
    _list_addresses_cmd,
    _privileged_port_advice,
    start_tftp_server,
)


def test_address_in_use_names_the_port_and_how_to_find_the_holder():
    msg = _bind_error_help("192.168.1.10", 69, OSError(errno.EADDRINUSE, "x"))
    assert "192.168.1.10:69" in msg
    assert "already in use" in msg
    assert "--tftp-via pod" in msg       # the way around it
    # and a command that actually exists on the host we are running on
    assert _find_port_holder_cmd(69) in msg


def test_address_not_available_points_at_the_missing_ip():
    msg = _bind_error_help("192.168.1.10", 69, OSError(errno.EADDRNOTAVAIL, "x"))
    assert "no interface has that address" in msg
    assert _add_address_cmd("192.168.1.10") in msg
    assert _list_addresses_cmd() in msg


def test_permission_denied_explains_the_refusal():
    msg = _bind_error_help("0.0.0.0", 69, OSError(errno.EACCES, "x"))
    assert "permission denied" in msg
    assert _privileged_port_advice() in msg


@pytest.mark.parametrize("plat", ["linux", "darwin", "win32"])
def test_advice_is_native_to_the_platform(monkeypatch, plat):
    """defib ships on Linux, macOS and Windows -- CI runs all three.

    Advising `ss`/`ip addr`/CAP_NET_BIND_SERVICE on a Mac or a Windows box
    sends the operator looking for tools that are not there, mid-recovery.
    """
    monkeypatch.setattr("defib.network.tftp_server.sys.platform", plat)
    holder = _find_port_holder_cmd(69)
    addr = _add_address_cmd("192.168.1.10")
    listing = _list_addresses_cmd()
    priv = _privileged_port_advice()

    if plat == "linux":
        assert "ss -ulpn" in holder and "ip addr add" in addr
        assert listing == "ip -brief address"
        assert "CAP_NET_BIND_SERVICE" in priv
    elif plat == "darwin":
        assert "lsof" in holder and "ifconfig" in addr
        assert listing == "ifconfig -a"
        # macOS reserves low ports but has no capabilities
        assert "sudo" in priv and "CAP_NET_BIND_SERVICE" not in priv
    else:
        assert "netstat" in holder and "netsh" in addr
        assert listing == "ipconfig"
        # Windows has no reserved-port rule -- do not tell people to be root
        assert "sudo" not in priv and "root" not in priv
        assert "firewall" in priv.lower()

    # no Linux-only tooling leaks into the non-Linux messages
    if plat != "linux":
        for m in (holder, addr, listing, priv):
            assert "ss -ulpn" not in m
            assert "ip addr add" not in m
            assert "CAP_NET_BIND_SERVICE" not in m


def test_unrecognised_errno_still_names_the_address():
    msg = _bind_error_help("10.0.0.1", 6969, OSError(errno.EPIPE, "broken pipe"))
    assert "10.0.0.1:6969" in msg
    assert "broken pipe" in msg


def test_start_tftp_server_raises_tftp_bind_error_on_a_taken_port():
    """End-to-end: the real bind path raises the typed error, not OSError."""
    async def go():
        # Hold an ephemeral UDP port, then ask the server for the same one.
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        taken = holder.getsockname()[1]
        try:
            with pytest.raises(TFTPBindError) as ei:
                await start_tftp_server(
                    file_data=b"x", bind_addr="127.0.0.1", port=taken,
                )
            msg = str(ei.value)
            assert f"127.0.0.1:{taken}" in msg
            assert "already in use" in msg
            # the original errno is preserved for callers that want it
            assert isinstance(ei.value.__cause__, OSError)
        finally:
            holder.close()

    asyncio.run(go())
