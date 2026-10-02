"""Tasmota smart-plug power controller.

Drives a single relay on a Tasmota (or API-compatible) smart plug via its
``/cm?cmnd=`` HTTP API.  Typical use is a mains plug feeding a camera's
power brick, or an inline USB relay.

Like :class:`~defib.power.rack.RackController`, each plug owns exactly
one camera, so the ``port`` argument is ignored.  Pass ``""`` from the CLI.

Power cycling is the base class's off / wait / on, with each switch
confirmed by the plug's reply.  A ``Backlog Power OFF; Delay N; Power ON``
looks tempting (timed on the plug), but its reply is ``{}`` whatever the
commands do, so it cannot be verified without polling — and on Tasmota
14.3 any command sent during the ``Delay`` cuts the delay short: the off
window shrank from ~4.5 s to ~2 s and the camera behind a mains brick
never booted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from defib.power.base import PowerController, PowerControllerError

logger = logging.getLogger(__name__)

class TasmotaController(PowerController):
    """Drives one relay of a Tasmota plug over HTTP."""

    def __init__(
        self,
        host: str,
        relay: int | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._relay = relay
        self._user = user
        self._password = password
        self._timeout = timeout

    @classmethod
    def name(cls) -> str:
        return "Tasmota plug"

    @classmethod
    def from_env(cls) -> TasmotaController:
        """Create from ``DEFIB_TASMOTA_*`` environment variables.

        Required:
            DEFIB_TASMOTA_HOST: plug IP or hostname (e.g. ``10.216.128.80``).
        Optional:
            DEFIB_TASMOTA_RELAY: relay index on multi-relay devices
                (``Power<N>``); omit for single-relay plugs.
            DEFIB_TASMOTA_USER / DEFIB_TASMOTA_PASSWORD: web credentials,
                when the plug has a web password set.
        """
        host = os.environ.get("DEFIB_TASMOTA_HOST")
        if not host:
            raise PowerControllerError(
                "DEFIB_TASMOTA_HOST env var required for Tasmota power control"
            )
        relay_env = os.environ.get("DEFIB_TASMOTA_RELAY")
        try:
            relay = int(relay_env) if relay_env else None
        except ValueError:
            raise PowerControllerError(
                f"DEFIB_TASMOTA_RELAY must be an integer, got {relay_env!r}"
            ) from None
        return cls(
            host=host,
            relay=relay,
            user=os.environ.get("DEFIB_TASMOTA_USER") or None,
            password=os.environ.get("DEFIB_TASMOTA_PASSWORD") or None,
        )

    @property
    def _power_cmd(self) -> str:
        return f"Power{self._relay}" if self._relay is not None else "Power"

    async def power_off(self, port: str) -> None:
        await self._set("OFF")

    async def power_on(self, port: str) -> None:
        await self._set("ON")

    async def close(self) -> None:
        # Stateless HTTP — nothing to release.
        return None

    async def _set(self, want: str) -> None:
        reply = await self._cmnd(f"{self._power_cmd} {want}")
        got = self._extract_state(reply)
        if got != want:
            raise PowerControllerError(
                f"Tasmota {self._host}: asked for {want}, plug reports {got!r}"
            )

    async def _state(self) -> str | None:
        return self._extract_state(await self._cmnd(self._power_cmd))

    def _extract_state(self, reply: dict[str, object]) -> str | None:
        # Single-relay plugs answer {"POWER": "ON"}; multi-relay ones
        # answer {"POWER2": "ON"}.  "Power1" may come back as "POWER".
        keys = [self._power_cmd.upper()]
        if self._relay in (None, 1):
            keys += ["POWER", "POWER1"]
        for key in keys:
            value = reply.get(key)
            if isinstance(value, str):
                return value.upper()
        return None

    async def _cmnd(self, command: str) -> dict[str, object]:
        params = {"cmnd": command}
        if self._user is not None:
            params["user"] = self._user
        if self._password is not None:
            params["password"] = self._password
        url = f"http://{self._host}/cm?{urllib.parse.urlencode(params)}"
        logger.info("tasmota GET %s cmnd=%r", self._host, command)
        return await asyncio.to_thread(self._get_sync, url, self._timeout)

    def _get_sync(self, url: str, timeout: float) -> dict[str, object]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            raise PowerControllerError(
                f"Tasmota {self._host}: HTTP {e.code}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PowerControllerError(
                f"Tasmota unreachable at {self._host}: {e}"
            ) from e
        try:
            result = json.loads(payload)
        except json.JSONDecodeError as e:
            raise PowerControllerError(
                f"Tasmota {self._host}: non-JSON reply {payload[:100]!r}"
            ) from e
        if not isinstance(result, dict):
            raise PowerControllerError(
                f"Tasmota {self._host}: unexpected reply {result!r}"
            )
        if "WARNING" in result:
            # Tasmota answers {"WARNING": "Need user=<username>&password=..."}
            # with HTTP 200 when the web password is wrong or missing.
            raise PowerControllerError(f"Tasmota {self._host}: {result['WARNING']}")
        return result
