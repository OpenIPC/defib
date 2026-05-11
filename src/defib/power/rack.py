"""Rack pod HTTP power controller.

The rack spinoff (``~/git/rack``) is a 100-node remote IP-camera lab
where each node is an ESP32-S3 "pod" that owns one camera and exposes
a small HTTP control API on ``:8080``.  This controller drives the
camera's high-side P-FET via ``/power/on``, ``/power/off``, and the
pod's built-in ``/power/cycle`` (2-second off interval).

Like :class:`~defib.power.vectis.VectisController`, each pod owns
exactly one camera, so port discovery is N/A and the ``port`` argument
to every method is ignored.  Pass ``""`` from the CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

from defib.power.base import PowerController, PowerControllerError

logger = logging.getLogger(__name__)


class RackController(PowerController):
    """Drives camera power on a rack pod via its HTTP API.

    Endpoints used (per ``rack/firmware/main/http_api.c``):

    - ``POST /power/on``    — drive high-side P-FET on
    - ``POST /power/off``   — drive high-side P-FET off
    - ``POST /power/cycle`` — pod-driven cycle, 2 s off interval

    HTTP is performed with :mod:`urllib.request` wrapped in
    :func:`asyncio.to_thread` — consistent with the firmware downloader
    in :mod:`defib.firmware`, zero extra dependencies.
    """

    def __init__(self, host: str, port: int = 8080, timeout: float = 10.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    @classmethod
    def name(cls) -> str:
        return "rack pod HTTP API"

    @classmethod
    def from_env(cls) -> RackController:
        """Create from ``DEFIB_RACK_*`` environment variables.

        Required:
            DEFIB_RACK_HOST: pod IP or mDNS hostname
                (e.g. ``10.216.128.69`` or ``rack-node-001.local``).
        Optional:
            DEFIB_RACK_PORT: HTTP API port (default 8080).
        """
        host = os.environ.get("DEFIB_RACK_HOST")
        if not host:
            raise PowerControllerError(
                "DEFIB_RACK_HOST env var required for rack power control"
            )
        return cls(
            host=host,
            port=int(os.environ.get("DEFIB_RACK_PORT", "8080")),
        )

    async def power_off(self, port: str) -> None:
        await self._post("/power/off")

    async def power_on(self, port: str) -> None:
        await self._post("/power/on")

    async def power_cycle(self, port: str, off_duration: float = 3.0) -> None:
        # The pod's /power/cycle uses a fixed 2 s off interval (long
        # enough to drain the camera's input cap for a clean cold-boot).
        # If the caller asks for materially longer, drive it ourselves.
        if off_duration <= 2.5:
            await self._post("/power/cycle")
        else:
            await self._post("/power/off")
            await asyncio.sleep(off_duration)
            await self._post("/power/on")

    async def close(self) -> None:
        # Stateless HTTP — nothing to release.
        return None

    async def tftp_put(
        self,
        name: str,
        data: bytes,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        """Stage a file for the pod's local TFTP server.

        The pod hosts a tiny TFTP server on its W5500 interface
        (``192.168.1.1:69``) — the camera's U-Boot can fetch staged
        files directly over the local LAN with no host-side TFTP
        server and no NAT44 in the data path.

        Args:
            name: filename the camera will request via ``tftp <addr> <name>``.
                Must not contain ``/``; the pod rejects path-traversal.
            data: raw file bytes.  PSRAM-allocated on the pod; max size
                is reported by the pod's ``GET /tftp`` (``max_size_bytes``).
            timeout: HTTP timeout in seconds.

        Returns:
            JSON from the pod, e.g. ``{"name": "uImage", "size": 2055676}``.
            On size/OOM the pod returns 4xx/503 with an ``error`` field —
            surfaced as ``PowerControllerError`` with the response body.
        """
        if "/" in name or not name:
            raise PowerControllerError(f"bad TFTP filename: {name!r}")
        url = f"http://{self._host}:{self._port}/tftp/{name}"
        logger.info("rack POST %s (%d bytes)", url, len(data))
        return await asyncio.to_thread(self._http_send_sync, "POST", url, data, timeout)

    async def tftp_delete(self, name: str, timeout: float = 10.0) -> dict[str, object]:
        """Drop a staged TFTP file on the pod."""
        if "/" in name:
            raise PowerControllerError(f"bad TFTP filename: {name!r}")
        path = f"/tftp/{name}" if name else "/tftp"
        url = f"http://{self._host}:{self._port}{path}"
        return await asyncio.to_thread(self._http_send_sync, "DELETE", url, b"", timeout)

    async def tftp_clear(self, timeout: float = 10.0) -> dict[str, object]:
        """Drop all staged TFTP files on the pod."""
        url = f"http://{self._host}:{self._port}/tftp"
        return await asyncio.to_thread(self._http_send_sync, "DELETE", url, b"", timeout)

    async def tftp_list(self, timeout: float = 10.0) -> dict[str, object]:
        """List staged TFTP files + PSRAM headroom."""
        url = f"http://{self._host}:{self._port}/tftp"
        return await asyncio.to_thread(self._http_send_sync, "GET", url, None, timeout)

    @staticmethod
    def _http_send_sync(
        method: str, url: str, body: bytes | None, timeout: float,
    ) -> dict[str, object]:
        req = urllib.request.Request(url, method=method)
        if body is not None:
            req.data = body
            req.add_header("Content-Type", "application/octet-stream")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise PowerControllerError(
                f"rack HTTP {e.code} on {method} {url}: {detail}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PowerControllerError(
                f"rack unreachable at {url}: {e}"
            ) from e
        try:
            result = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return result if isinstance(result, dict) else {}

    async def fastboot(
        self,
        spl_address: int,
        ddr_step_address: int,
        uboot_address: int,
        prestep0: bytes,
        ddrstep0: bytes,
        prestep1: bytes | None,
        spl: bytes,
        agent: bytes,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        """Run the entire HiSilicon SPL BootROM upload locally on the pod.

        Packs profile + SPL + agent into the binary blob the pod's
        ``POST /fastboot`` expects, sends it, and returns the parsed JSON
        response. The pod takes exclusive UART access for the upload,
        so no concurrent TCP UART client may be active.

        Response shape (success):
            {"success": true, "last_phase": "done", "elapsed_ms": ...,
             "handshake_markers": N}
        Response shape (failure): adds "failed_phase" + "error".
        """
        blob = bytearray()
        blob += spl_address.to_bytes(4, "big")
        blob += ddr_step_address.to_bytes(4, "big")
        blob += uboot_address.to_bytes(4, "big")
        blob += len(prestep0).to_bytes(2, "big")
        blob += prestep0
        blob += len(ddrstep0).to_bytes(2, "big")
        blob += ddrstep0
        p1 = prestep1 or b""
        blob += len(p1).to_bytes(2, "big")
        blob += p1
        blob += len(spl).to_bytes(4, "big")
        blob += spl
        blob += len(agent).to_bytes(4, "big")
        blob += agent

        url = f"http://{self._host}:{self._port}/fastboot"
        logger.info("rack POST %s (%d bytes blob)", url, len(blob))
        return await asyncio.to_thread(self._post_blob_sync, url, bytes(blob), timeout)

    def _post_blob_sync(
        self, url: str, body: bytes, timeout: float
    ) -> dict[str, object]:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            # Pod returns 500 + JSON body on protocol failure — surface
            # the JSON so callers can read failed_phase/error.
            payload = e.read()
            try:
                result = json.loads(payload)
                return result if isinstance(result, dict) else {}
            except json.JSONDecodeError:
                raise PowerControllerError(
                    f"rack HTTP {e.code} on {url}: "
                    f"{payload.decode('utf-8', 'replace')[:200]}"
                ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PowerControllerError(
                f"rack unreachable at {url}: {e}"
            ) from e
        try:
            result = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return result if isinstance(result, dict) else {}

    async def _post(self, path: str) -> dict[str, object]:
        url = f"http://{self._host}:{self._port}{path}"
        logger.info("rack POST %s", url)
        return await asyncio.to_thread(self._post_sync, url)

    def _post_sync(self, url: str) -> dict[str, object]:
        req = urllib.request.Request(url, method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise PowerControllerError(
                f"rack HTTP {e.code} on {url}: {detail}"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PowerControllerError(
                f"rack unreachable at {url}: {e}"
            ) from e
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            return {}
        return result if isinstance(result, dict) else {}
