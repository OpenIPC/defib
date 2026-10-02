"""Generic HTTP relay power controller.

For any relay that exposes "turn on" and "turn off" as plain GET URLs —
small bridge services in front of cloud-only smart switches (e.g. a local
eWeLink/Sonoff bridge at ``http://127.0.0.1:8090/relay/on``), ESPHome
web-server buttons, homegrown relay boards.

Single-port, like :class:`~defib.power.rack.RackController`: the ``port``
argument is ignored.  Power cycling is timed on the host, so it inherits
whatever latency the relay's backend adds.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from defib.power.base import PowerController, PowerControllerError

logger = logging.getLogger(__name__)


class HttpRelayController(PowerController):
    """Drives power by GETting one URL for on and another for off."""

    def __init__(self, on_url: str, off_url: str, timeout: float = 10.0) -> None:
        self._on_url = on_url
        self._off_url = off_url
        self._timeout = timeout

    @classmethod
    def name(cls) -> str:
        return "HTTP relay"

    @classmethod
    def from_env(cls) -> HttpRelayController:
        """Create from ``DEFIB_HTTP_POWER_*`` environment variables.

        Required:
            DEFIB_HTTP_POWER_ON_URL: GET this to switch power on.
            DEFIB_HTTP_POWER_OFF_URL: GET this to switch power off.
        Optional:
            DEFIB_HTTP_POWER_TIMEOUT: per-request timeout in seconds
                (default 10).
        """
        on_url = os.environ.get("DEFIB_HTTP_POWER_ON_URL")
        off_url = os.environ.get("DEFIB_HTTP_POWER_OFF_URL")
        if not on_url or not off_url:
            raise PowerControllerError(
                "DEFIB_HTTP_POWER_ON_URL and DEFIB_HTTP_POWER_OFF_URL env vars "
                "required for HTTP relay power control"
            )
        timeout_env = os.environ.get("DEFIB_HTTP_POWER_TIMEOUT", "10")
        try:
            timeout = float(timeout_env)
        except ValueError:
            raise PowerControllerError(
                f"DEFIB_HTTP_POWER_TIMEOUT must be a number, got {timeout_env!r}"
            ) from None
        return cls(on_url=on_url, off_url=off_url, timeout=timeout)

    async def power_off(self, port: str) -> None:
        await asyncio.to_thread(self._get_sync, self._off_url)

    async def power_on(self, port: str) -> None:
        await asyncio.to_thread(self._get_sync, self._on_url)

    async def close(self) -> None:
        # Stateless HTTP — nothing to release.
        return None

    def _get_sync(self, url: str) -> None:
        shown = _redact(url)
        logger.info("http relay GET %s", shown)
        try:
            with urllib.request.urlopen(_request(url), timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            detail = _redact_text(e.read().decode("utf-8", "replace")[:300], url)
            raise PowerControllerError(
                f"HTTP relay {e.code} on GET {shown}: {detail}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PowerControllerError(
                f"HTTP relay unreachable at {shown}: {_redact_text(str(e), url)}"
            ) from e


def _request(url: str) -> urllib.request.Request:
    """A GET for ``url``, sending any ``user:pass@`` as HTTP Basic auth.

    urllib does not do that itself: it would try to connect to a host
    literally named ``user:pass@host``.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.username is None:
        return urllib.request.Request(url)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    bare = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    creds = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
    token = base64.b64encode(creds.encode()).decode()
    return urllib.request.Request(bare, headers={"Authorization": f"Basic {token}"})


def _redact(url: str) -> str:
    """The URL without user info or query, which may carry credentials."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    query = "?..." if parts.query else ""
    return f"{parts.scheme}://{host}{parts.path}{query}"


def _redact_text(text: str, url: str) -> str:
    """Strip the URL's secrets from a message that may echo them."""
    parts = urllib.parse.urlsplit(url)
    secrets = [parts.password, parts.query]
    secrets += [value for _, value in urllib.parse.parse_qsl(parts.query)]
    # Longest first, so a query value never leaves part of the full query.
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        # Skip trivially short values ("1", "on"): blanking them would
        # mangle the message without hiding anything worth hiding.
        if len(secret) >= 3:
            text = text.replace(secret, "...")
    return text
