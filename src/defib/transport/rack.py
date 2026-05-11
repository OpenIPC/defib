"""TCP transport for rack pods, with out-of-band baud rate control.

A rack pod's TCP UART bridge passes bytes verbatim — no in-band signal
for the bridge to change its UART baud rate. The pod exposes a separate
HTTP control plane (``POST /uart/baud``) for that, so callers like
:class:`~defib.agent.client.FlashAgentClient.set_baud` can sync both
ends of the link when the on-device agent jumps to a faster rate.

``RackTransport`` extends :class:`~defib.transport.socket.SocketTransport`
with the HTTP base URL of the controlling pod and an
:meth:`set_baudrate` override that POSTs the new rate. URL scheme:

    ``rack://host[:bridge_port][?api=http_port]``

defaults: ``bridge_port=9000``, ``http_port=8080``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket as sock_mod
import urllib.error
import urllib.request

from defib.transport.base import TransportError
from defib.transport.socket import SocketTransport

logger = logging.getLogger(__name__)


class RackTransport(SocketTransport):
    """SocketTransport + HTTP control channel for the pod's /uart/baud."""

    def __init__(self, conn: sock_mod.socket, http_base: str) -> None:
        super().__init__(conn)
        self._http_base = http_base.rstrip("/")

    @classmethod
    async def create_rack(
        cls,
        host: str,
        bridge_port: int = 9000,
        http_port: int = 8080,
    ) -> RackTransport:
        try:
            s = sock_mod.socket(sock_mod.AF_INET, sock_mod.SOCK_STREAM)
            s.setblocking(False)
            s.setsockopt(sock_mod.IPPROTO_TCP, sock_mod.TCP_NODELAY, 1)
            loop = asyncio.get_event_loop()
            await loop.sock_connect(s, (host, bridge_port))
        except OSError as e:
            raise TransportError(
                f"Failed to connect to rack pod {host}:{bridge_port}: {e}"
            ) from e
        http_base = f"http://{host}:{http_port}"
        logger.info(
            "Connected to rack pod: tcp://%s:%d (control %s)",
            host, bridge_port, http_base,
        )
        return cls(s, http_base)

    async def set_baudrate(self, baud: int) -> None:
        """Sync the pod's UART side to ``baud`` via POST /uart/baud.

        The on-device agent flips to ``baud`` after its own CMD_SET_BAUD
        handler; we POST here to bring the bridge's UART side in line.
        Without this, the host writes at host-imagined ``baud`` but the
        bridge keeps clocking at 115200 — every byte gets mangled.
        """
        url = f"{self._http_base}/uart/baud"
        body = json.dumps({"rate": int(baud)}).encode("ascii")
        logger.info("rack POST %s rate=%d", url, baud)
        await asyncio.to_thread(self._post_baud_sync, url, body)

    @staticmethod
    def _post_baud_sync(url: str, body: bytes) -> None:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise TransportError(
                f"rack HTTP {e.code} on {url}: {detail}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TransportError(
                f"rack unreachable at {url}: {e}"
            ) from e
