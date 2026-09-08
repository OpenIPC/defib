"""Cross-platform temporary static IP address management.

Assigns and removes temporary static IP addresses on network interfaces
for TFTP-based device recovery. The device typically expects the host
at a specific IP (e.g., 192.168.1.10).

Platform support:
- Linux: ip addr add/del
- macOS: ifconfig alias
- Windows: netsh interface ip add/delete address

Two Windows facts shape most of what follows, both reported in
OpenIPC/firmware#2381. `netsh` addresses adapters by their *friendly* name
("Ethernet"), which is not what ``socket.if_nameindex()`` returns there
("ethernet_0"), so the names have to come from netsh itself. And netsh returns
before the stack can actually bind the address, so an immediate bind fails with
EADDRNOTAVAIL on an address that is on its way up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class IPManagerError(Exception):
    """Failed to manage IP address."""


# netsh is the only source of adapter names netsh will accept.
_NETSH_LIST_CMD = ["netsh", "interface", "show", "interface"]
_NETSH_TIMEOUT = 10


async def _run_command(cmd: list[str]) -> tuple[int, str, str]:
    """Run a shell command asynchronously and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


def _bindable(ip: str) -> bool:
    """Can a socket bind this address right now?

    This is the property every caller actually wants -- the TFTP server binds
    the address it just assigned -- and unlike parsing command output it is
    language-independent, which matters because netsh and `ip` are localised.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((ip, 0))
        except OSError:
            return False
    return True


async def _wait_until_bindable(ip: str, timeout: float = 10.0) -> bool:
    """Poll until the address is usable, or the deadline passes.

    Windows returns from `netsh interface ip add address` before the address
    is plumbed in, so the bind that follows raced it and lost -- and because
    the failure unwound the context manager, the log showed defib removing the
    address it had just added, which reads like the tool sabotaging itself
    (OpenIPC/firmware#2381).
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if _bindable(ip):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


async def add_ip(interface: str, ip: str, netmask: str = "255.255.255.0") -> bool:
    """Add a temporary static IP address to a network interface.

    Returns True if this call assigned the address, False if it was already
    there. The caller needs the difference: an address we did not add is not
    ours to take away again.

    Args:
        interface: Network interface name (e.g., "eth0", "en0", "Ethernet").
        ip: IP address to assign (e.g., "192.168.1.10").
        netmask: Subnet mask (default "255.255.255.0").

    Raises:
        IPManagerError: If the address cannot be made usable.
    """
    prefix = _netmask_to_prefix(netmask)

    # Already configured -- by a previous run, by the operator setting it up by
    # hand, or by a NIC that keeps it. All three are the state we wanted, so
    # succeed and remember that the teardown must leave it alone.
    if _bindable(ip):
        logger.info("%s is already usable; leaving it as it is", ip)
        return False

    if sys.platform == "linux":
        cmd = ["ip", "addr", "add", f"{ip}/{prefix}", "dev", interface]
    elif sys.platform == "darwin":
        cmd = ["ifconfig", interface, "alias", ip, "netmask", netmask]
    elif sys.platform == "win32":
        cmd = ["netsh", "interface", "ip", "add", "address", interface, ip, netmask]
    else:
        raise IPManagerError(f"Unsupported platform: {sys.platform}")

    logger.info("Adding IP %s/%d to %s: %s", ip, prefix, interface, " ".join(cmd))
    returncode, stdout, stderr = await _run_command(cmd)

    if returncode != 0:
        detail = stderr.strip() or stdout.strip()
        # The command can fail and the address still be there: another process
        # won the race, or the platform reports "already exists" as an error.
        # Bindability is the answer, not the exit status.
        if _bindable(ip):
            logger.info("%s reported an error but %s is usable: %s", cmd[0], ip, detail)
            return False
        advice = await _interface_name_advice(interface)
        raise IPManagerError(f"Failed to add IP {ip} to {interface}: {detail}\n{advice}")

    # From here the address is ours, so every way out of this function has to
    # take it back down again -- `temporary_ip` cannot, because it does not
    # learn that we own it until we return. Leaving it up would also make the
    # next run read it as the operator's and never clean it up.
    try:
        ready = await _wait_until_bindable(ip)
    except BaseException:
        await _undo_add(interface, ip, netmask)
        raise
    if not ready:
        await _undo_add(interface, ip, netmask)
        raise IPManagerError(
            f"{ip} was assigned to {interface} but never became usable. "
            f"Check that the adapter is connected and that {ip} does not "
            f"collide with an address already on this host."
        )
    logger.info("Successfully added %s to %s", ip, interface)
    return True


async def _undo_add(interface: str, ip: str, netmask: str) -> None:
    """Best-effort rollback that must never replace the error that caused it."""
    try:
        await remove_ip(interface, ip, netmask)
    except Exception:  # noqa: BLE001 - rollback failure must not mask the cause
        logger.warning("Could not take %s back off %s", ip, interface, exc_info=True)


async def _interface_name_advice(interface: str) -> str:
    """Point at the name mismatch that is nearly always the Windows cause."""
    if sys.platform != "win32":
        return f"Check that {interface} exists -- `ip -brief link` lists what does."
    names = ", ".join(await list_interfaces_async()) or "none found"
    return (
        f"On Windows netsh wants the adapter's friendly name, which is not the "
        f"name Python reports for it. Adapters on this host: {names}. "
        f"Pass one with --nic."
    )


async def remove_ip(interface: str, ip: str, netmask: str = "255.255.255.0") -> None:
    """Remove a temporary static IP address from a network interface.

    Args:
        interface: Network interface name.
        ip: IP address to remove.
        netmask: Subnet mask.
    """
    prefix = _netmask_to_prefix(netmask)

    if sys.platform == "linux":
        cmd = ["ip", "addr", "del", f"{ip}/{prefix}", "dev", interface]
    elif sys.platform == "darwin":
        cmd = ["ifconfig", interface, "-alias", ip]
    elif sys.platform == "win32":
        cmd = ["netsh", "interface", "ip", "delete", "address", interface, ip]
    else:
        raise IPManagerError(f"Unsupported platform: {sys.platform}")

    logger.info("Removing IP %s from %s: %s", ip, interface, " ".join(cmd))
    returncode, stdout, stderr = await _run_command(cmd)

    if returncode != 0:
        logger.warning("Failed to remove IP %s from %s: %s", ip, interface, stderr.strip())
    else:
        logger.info("Successfully removed %s from %s", ip, interface)


@asynccontextmanager
async def temporary_ip(
    interface: str,
    ip: str,
    netmask: str = "255.255.255.0",
) -> AsyncIterator[str]:
    """Context manager that assigns a temporary IP and removes it on exit.

    Usage:
        async with temporary_ip("eth0", "192.168.1.10") as ip:
            # ip is now assigned to eth0
            await do_tftp_recovery()
        # ip is automatically removed

    Args:
        interface: Network interface name.
        ip: IP address to assign temporarily.
        netmask: Subnet mask.

    Yields:
        The assigned IP address.
    """
    added = await add_ip(interface, ip, netmask)
    try:
        yield ip
    finally:
        if added:
            await remove_ip(interface, ip, netmask)
        else:
            logger.info("Leaving %s on %s: this run did not add it", ip, interface)


def _netmask_to_prefix(netmask: str) -> int:
    """Convert dotted netmask to CIDR prefix length."""
    parts = netmask.split(".")
    if len(parts) != 4:
        return 24
    binary = "".join(f"{int(p):08b}" for p in parts)
    return binary.count("1")


def _windows_interfaces() -> list[str]:
    """Adapter names in the form netsh will accept.

    `socket.if_nameindex()` answers on Windows, which is what made this look
    like it worked: it returns "ethernet_0", netsh has never heard of that, and
    the failure it gives back is "Failed to configure the DHCP service. The
    interface may be disconnected." -- which sends the reader after a cable.
    The reporter in OpenIPC/firmware#2381 got there by renaming their adapter
    to `ethernet_0` to match. Ask netsh instead, so the names we hand out are
    the names we hand back.

    Parsed positionally rather than by header text, because netsh is localised:

        Admin State    State          Type             Interface Name
        -----------------------------------------------------------------
        Enabled        Connected      Dedicated        Ethernet

    Three single-token columns, then a name that may contain spaces.
    """
    import subprocess

    try:
        proc = subprocess.run(
            _NETSH_LIST_CMD,
            capture_output=True, text=True, timeout=_NETSH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not list adapters with netsh: %s", exc)
        return []

    # Check the exit status, as the async twin does. Without this a netsh that
    # failed left us parsing empty stdout and reporting "no adapters", which
    # looks like a host with no network rather than a command that did not run.
    if proc.returncode != 0:
        logger.warning("netsh could not list adapters: %s", proc.stderr.strip())
        return []

    return _parse_netsh_interfaces(proc.stdout)


async def _windows_interfaces_async() -> list[str]:
    """As `_windows_interfaces`, without standing on the event loop.

    `list_interfaces()` is reached from the async install and network paths,
    and from add_ip's own failure advice. A synchronous `subprocess.run` there
    stops every other task until netsh returns -- which is the whole recovery,
    including the serial link to a camera sitting in its bootrom window.
    """
    try:
        returncode, stdout, stderr = await asyncio.wait_for(
            _run_command(_NETSH_LIST_CMD), timeout=_NETSH_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Could not list adapters with netsh: %s", exc)
        return []
    if returncode != 0:
        logger.warning("netsh could not list adapters: %s", stderr.strip())
        return []
    return _parse_netsh_interfaces(stdout)


def _parse_netsh_interfaces(stdout: str) -> list[str]:
    """Pull the adapter names out of a `netsh interface show interface` table."""
    names: list[str] = []
    seen_separator = False
    for line in stdout.splitlines():
        if line.strip().startswith("---"):
            # Everything above the rule is the header, in whatever language.
            seen_separator = True
            continue
        if not seen_separator:
            continue
        fields = line.split(None, 3)
        if len(fields) != 4:
            continue
        name = fields[3].strip()
        if name and name not in names:
            names.append(name)
    return names


async def list_interfaces_async() -> list[str]:
    """`list_interfaces` for callers that are already in an event loop."""
    if sys.platform == "win32":
        return await _windows_interfaces_async() or _fallback_interfaces()
    return list_interfaces()


def _fallback_interfaces() -> list[str]:
    """Last resort when the platform will not enumerate."""
    if sys.platform == "linux":
        return ["eth0", "enp0s3"]
    if sys.platform == "darwin":
        return ["en0", "en1"]
    if sys.platform == "win32":
        return ["Ethernet", "Wi-Fi"]
    return []


def list_interfaces() -> list[str]:
    """List available network interfaces.

    Returns interface names that can be used with add_ip/remove_ip -- which is
    the whole point, and is why Windows does not go through if_nameindex.

    Synchronous, so it belongs to the synchronous CLI commands;
    `list_interfaces_async` is the one to reach for inside a coroutine.
    """
    import socket

    interfaces: list[str] = []

    if sys.platform == "win32":
        interfaces = _windows_interfaces()
    else:
        try:
            if hasattr(socket, "if_nameindex"):
                for _, name in socket.if_nameindex():
                    if name != "lo":
                        interfaces.append(name)
        except OSError:
            pass

    return interfaces or _fallback_interfaces()
