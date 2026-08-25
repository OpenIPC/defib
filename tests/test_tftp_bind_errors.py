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
    _bind_error_help,
    start_tftp_server,
)


def test_address_in_use_names_the_port_and_how_to_find_the_holder():
    msg = _bind_error_help("192.168.1.10", 69, OSError(errno.EADDRINUSE, "x"))
    assert "192.168.1.10:69" in msg
    assert "already in use" in msg
    assert "sport = :69" in msg          # the command that finds the holder
    assert "--tftp-via pod" in msg       # the way around it


def test_address_not_available_points_at_the_missing_ip():
    msg = _bind_error_help("192.168.1.10", 69, OSError(errno.EADDRNOTAVAIL, "x"))
    assert "no interface has that address" in msg
    assert "ip addr add 192.168.1.10/24" in msg


def test_permission_denied_explains_the_privileged_port():
    msg = _bind_error_help("0.0.0.0", 69, OSError(errno.EACCES, "x"))
    assert "permission denied" in msg
    assert "1024" in msg


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
