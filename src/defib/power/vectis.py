"""OpenIPC Vectis UART bridge power controller.

Vectis (https://github.com/OpenIPC/vectis) is a USB/Ethernet UART
bridge that exposes the camera's UART over TCP and gates camera power
via the bridge's RTS/DTR lines.  Vectis ≥ 1.2.0 speaks RFC 2217 on the
listener: clients negotiate Telnet binary mode and drive RTS/DTR via
``SET-CONTROL`` sub-options instead of in-band magic bytes, so the
data path stays binary safe even for firmware blobs that contain the
old Ctrl+P (``0x10``) byte.

This controller pulses RTS+DTR off → 200 ms → on through the
:class:`Rfc2217Transport` it shares with the recovery session.  Two
properties of the bridge dictate the design:

1. The camera is powered only while a TCP client is connected.  If
   the socket is closed the camera loses power.
2. The bridge accepts only one remote client at a time.

Both are satisfied because defib uses the *same* RFC 2217 connection
for the UART and the modem-control commands.
"""

from __future__ import annotations

import asyncio
import logging
import os

from defib.power.base import PowerController, PowerControllerError
from defib.transport.base import Transport
from defib.transport.rfc2217 import Rfc2217Transport

logger = logging.getLogger(__name__)


class VectisController(PowerController):
    """Drives camera power on the OpenIPC Vectis UART bridge.

    Two operating modes:

    - **Shared transport** (preferred for live recovery): the CLI
      hands a live :class:`Rfc2217Transport` to the controller via
      :meth:`attach_transport`.  ``power_cycle`` toggles RTS+DTR
      through it (RFC 2217 ``SET-CONTROL`` sub-options 8/9 + 11/12).
      The transport stays open so the camera stays powered for the
      whole session and the data path remains binary safe.

    - **Standalone**: no transport attached.  ``power_cycle`` opens a
      short-lived RFC 2217 connection, pulses RTS/DTR, and closes.
      Useful for ad-hoc ``defib power cycle``-style debug commands.
      Closing the connection cuts camera power, so this mode is not
      suitable for driving a recovery flow.
    """

    def __init__(
        self,
        host: str,
        port: int = 35240,
        transport: Transport | None = None,
        pulse_seconds: float = 0.25,
    ) -> None:
        self._host = host
        self._port = port
        self._transport: Transport | None = transport
        self._owns_transport = False
        self._pulse_seconds = pulse_seconds

    @classmethod
    def name(cls) -> str:
        return "OpenIPC Vectis UART bridge"

    @classmethod
    def from_env(cls) -> VectisController:
        """Create from ``DEFIB_VECTIS_*`` environment variables.

        Required:
            DEFIB_VECTIS_HOST: Vectis bridge IP/hostname.
        Optional:
            DEFIB_VECTIS_PORT: TCP listener port (default 35240).
        """
        host = os.environ.get("DEFIB_VECTIS_HOST")
        if not host:
            raise PowerControllerError(
                "DEFIB_VECTIS_HOST env var required for Vectis power control"
            )
        return cls(
            host=host,
            port=int(os.environ.get("DEFIB_VECTIS_PORT", "35240")),
        )

    def attach_transport(self, transport: Transport) -> None:
        """Use a live shared transport for the RTS/DTR toggle.

        Expected to be an :class:`Rfc2217Transport`; any transport
        exposing ``set_dtr``/``set_rts`` will work.
        """
        self._transport = transport
        self._owns_transport = False

    async def power_off(self, port: str) -> None:
        raise PowerControllerError(
            "Vectis pulses power; off/on are not separately addressable. "
            "Use power_cycle()."
        )

    async def power_on(self, port: str) -> None:
        raise PowerControllerError(
            "Vectis pulses power; off/on are not separately addressable. "
            "Use power_cycle()."
        )

    async def power_cycle(self, port: str, off_duration: float = 0.0) -> None:
        """Pulse RTS+DTR off → ``pulse_seconds`` → on.

        ``port`` and ``off_duration`` are ignored — Vectis controls a
        single attached camera and the pulse width comes from
        ``self._pulse_seconds`` (default 250 ms, comfortable margin
        over the 200 ms inverted-pulse the bridge generates from a
        single Ctrl+P in legacy mode).
        """
        if self._transport is None:
            await self._open_standalone()
            try:
                await self._do_pulse()
            finally:
                await self._close_standalone()
        else:
            await self._do_pulse()

    async def _do_pulse(self) -> None:
        transport = self._transport
        assert transport is not None
        logger.info(
            "Vectis power-cycle: SET-CONTROL DTR/RTS on %s:%d (off → %.0f ms → on)",
            self._host, self._port, self._pulse_seconds * 1000,
        )

        set_dtr = getattr(transport, "set_dtr", None)
        set_rts = getattr(transport, "set_rts", None)
        if set_dtr is None or set_rts is None:
            # Fallback for transports that don't expose modem-control —
            # only meaningful against a pre-RFC-2217 Vectis daemon.
            logger.warning(
                "Transport lacks set_dtr/set_rts; falling back to Ctrl+P byte"
            )
            await transport.write(b"\x10")
            await asyncio.sleep(self._pulse_seconds)
            return

        await set_dtr(False)
        await set_rts(False)
        await asyncio.sleep(self._pulse_seconds)
        await set_rts(True)
        await set_dtr(True)

    async def _open_standalone(self) -> None:
        url = f"rfc2217://{self._host}:{self._port}"
        self._transport = await Rfc2217Transport.create(url)
        self._owns_transport = True

    async def _close_standalone(self) -> None:
        if self._transport is not None and self._owns_transport:
            await self._transport.close()
            self._transport = None
            self._owns_transport = False

    async def close(self) -> None:
        # Only close transports we opened ourselves.  In shared-transport
        # mode the CLI owns the transport.
        await self._close_standalone()
