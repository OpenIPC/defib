"""USB hub port power controller (``uhubctl``).

For boards that draw their power from a USB port: a hub that supports
per-port power switching (``ppps``) can cut VBUS to that one port, which is
a hard power cycle.  This drives the `uhubctl <https://github.com/mvp/uhubctl>`_
command-line tool rather than libusb directly, so the hub compatibility
list and its quirks stay uhubctl's problem.

Like :class:`~defib.power.tasmota.TasmotaController`, one controller owns
exactly one hub port, chosen by ``DEFIB_UHUBCTL_LOCATION`` and
``DEFIB_UHUBCTL_PORT``; the ``port`` argument is ignored.

A USB 3 hub enumerates as two hubs (a USB 2 one and its USB 3 twin, e.g.
``3-2`` and ``4-2``).  uhubctl switches both when given either location,
and prints a ``New status`` block for each.  Every switch is confirmed from
those blocks: the port must read ``off`` (or ``power``) in all of them, so
a switch that did not happen is an error, not a silent no-op.

The port status uhubctl reports is what the hub *says*.  A hub that claims
``ppps`` but gangs VBUS in hardware will report ``off`` while the board
stays powered; the only check for that is the board itself going quiet.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex

from defib.power.base import PowerController, PowerControllerError

logger = logging.getLogger(__name__)

# "New status for hub 3-2.3 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]"
_HUB_HEADER = re.compile(r"^(Current|New) status for hub (\S+) \[(.*)\]\s*$")
# "  Port 3: 0100 power" / "  Port 3: 0000 off" / "  Port 1: 02a0 power 5gbps Rx.Detect"
_PORT_LINE = re.compile(r"^\s+Port (\d+): ([0-9a-fA-F]{4})\s*(.*)$")


def parse_new_status(output: str, port: int) -> list[tuple[str, str, str]]:
    """Return ``(hub, flags, hub_description)`` for *port* in each
    ``New status`` block of uhubctl's output.

    ``flags`` is the text after the hex status word, e.g. ``"power"`` or
    ``"off"``.  ``Current status`` blocks (the state before the action) are
    skipped.
    """
    results: list[tuple[str, str, str]] = []
    in_new = False
    hub = desc = ""
    for line in output.splitlines():
        header = _HUB_HEADER.match(line)
        if header:
            in_new = header.group(1) == "New"
            hub, desc = header.group(2), header.group(3)
            continue
        if not in_new:
            continue
        m = _PORT_LINE.match(line)
        if m and int(m.group(1)) == port:
            results.append((hub, m.group(3).strip(), desc))
    return results


class UhubctlController(PowerController):
    """Switches VBUS on one port of a USB hub through ``uhubctl``."""

    def __init__(
        self,
        location: str,
        port: int,
        command: list[str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._location = location
        self._port = port
        self._command = command or ["uhubctl"]
        self._timeout = timeout

    @classmethod
    def name(cls) -> str:
        return "USB hub port (uhubctl)"

    @classmethod
    def from_env(cls) -> UhubctlController:
        """Create from ``DEFIB_UHUBCTL_*`` environment variables.

        Required:
            DEFIB_UHUBCTL_LOCATION: hub location as uhubctl prints it
                (e.g. ``3-2.3``).  Either twin of a USB 3 hub works.
            DEFIB_UHUBCTL_PORT: port number on that hub (e.g. ``3``).
        Optional:
            DEFIB_UHUBCTL_COMMAND: command to run instead of ``uhubctl``,
                split like a shell would (e.g. ``sudo /usr/local/bin/uhubctl``)
                for hosts without a udev rule granting hub access.
        """
        location = os.environ.get("DEFIB_UHUBCTL_LOCATION", "").strip()
        if not location:
            raise PowerControllerError(
                "DEFIB_UHUBCTL_LOCATION env var required for uhubctl power "
                "control (hub location as `uhubctl` lists it, e.g. 3-2.3)"
            )
        port_env = os.environ.get("DEFIB_UHUBCTL_PORT", "").strip()
        if not port_env:
            raise PowerControllerError(
                "DEFIB_UHUBCTL_PORT env var required for uhubctl power control"
            )
        try:
            port = int(port_env)
        except ValueError:
            raise PowerControllerError(
                f"DEFIB_UHUBCTL_PORT must be an integer, got {port_env!r}"
            ) from None
        if port < 1:
            raise PowerControllerError(
                f"DEFIB_UHUBCTL_PORT must be 1 or more, got {port}"
            )
        command_env = os.environ.get("DEFIB_UHUBCTL_COMMAND", "").strip()
        try:
            command = shlex.split(command_env) if command_env else None
        except ValueError as e:
            raise PowerControllerError(
                f"DEFIB_UHUBCTL_COMMAND is not a valid command line "
                f"({e}): {command_env!r}"
            ) from None
        if command == []:
            command = None
        return cls(location=location, port=port, command=command)

    async def power_off(self, port: str) -> None:
        await self._switch("off")

    async def power_on(self, port: str) -> None:
        await self._switch("on")

    async def close(self) -> None:
        # One subprocess per switch -- nothing held open.
        return None

    async def _switch(self, action: str) -> None:
        output = await self._run(
            ["-l", self._location, "-p", str(self._port), "-a", action]
        )
        statuses = parse_new_status(output, self._port)
        where = f"hub {self._location} port {self._port}"
        if not statuses:
            raise PowerControllerError(
                f"uhubctl {where}: no new port status in its output: "
                f"{output.strip()[-300:]!r}"
            )
        want = "off" if action == "off" else "power"
        for hub, flags, desc in statuses:
            words = flags.split()
            if want not in words:
                raise PowerControllerError(
                    f"uhubctl {where}: asked for {action}, hub {hub} reports "
                    f"{flags!r}"
                )
            if "ppps" not in desc:
                logger.warning(
                    "uhubctl: hub %s does not report per-port power switching "
                    "(%s); the board may stay powered", hub, desc,
                )

    async def _run(self, args: list[str]) -> str:
        argv = [*self._command, *args]
        logger.info("uhubctl: %s", shlex.join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            raise PowerControllerError(
                f"uhubctl not found ({self._command[0]!r}); install it or set "
                "DEFIB_UHUBCTL_COMMAND"
            ) from e
        except OSError as e:
            raise PowerControllerError(
                f"cannot run {self._command[0]!r}: {e}"
            ) from e
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self._timeout)
        except asyncio.TimeoutError as e:
            raise PowerControllerError(
                f"uhubctl timed out after {self._timeout:g} s"
            ) from e
        finally:
            # On a timeout or a cancelled caller (Ctrl-C), uhubctl must not
            # go on switching the port after we have stopped waiting for it.
            if proc.returncode is None:
                proc.kill()
                await asyncio.shield(proc.wait())
        text = out.decode(errors="replace")
        if proc.returncode != 0:
            raise PowerControllerError(
                f"uhubctl exited {proc.returncode}: {text.strip()[-300:]}"
            )
        return text
