"""Tests for the rack-pod HTTP power controller."""

from __future__ import annotations

import io
import json
import urllib.error
from contextlib import contextmanager
from typing import Any

import pytest

from defib.power import rack as rack_mod
from defib.power.base import PowerControllerError
from defib.power.factory import power_controller_from_env
from defib.power.rack import RackController


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    """Minimal context-manager response for urllib.request.urlopen()."""

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class UrlopenRecorder:
    """Capture every urlopen call so we can assert on URL and order."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.body = body

    def __call__(self, req: Any, timeout: float | None = None) -> FakeResponse:
        url = req.full_url
        method = req.get_method()
        data = req.data
        self.calls.append((method, url, data))
        return FakeResponse(self.body)


@contextmanager
def patched_urlopen(monkeypatch: pytest.MonkeyPatch, body: bytes = b"{}") -> Any:
    rec = UrlopenRecorder(body)
    monkeypatch.setattr(rack_mod.urllib.request, "urlopen", rec)
    yield rec


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------

class TestRackFromEnv:
    def test_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFIB_RACK_HOST", raising=False)
        with pytest.raises(PowerControllerError, match="DEFIB_RACK_HOST"):
            RackController.from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_RACK_HOST", "10.216.128.69")
        monkeypatch.delenv("DEFIB_RACK_PORT", raising=False)
        ctrl = RackController.from_env()
        assert ctrl._host == "10.216.128.69"
        assert ctrl._port == 8080

    def test_custom_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_RACK_HOST", "rack-node-001.local")
        monkeypatch.setenv("DEFIB_RACK_PORT", "9090")
        ctrl = RackController.from_env()
        assert ctrl._host == "rack-node-001.local"
        assert ctrl._port == 9090

    def test_factory_dispatches_rack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_POWER_TYPE", "rack")
        monkeypatch.setenv("DEFIB_RACK_HOST", "10.0.0.5")
        ctrl = power_controller_from_env()
        assert isinstance(ctrl, RackController)


# ---------------------------------------------------------------------------
# Power on/off/cycle issue the expected HTTP POSTs
# ---------------------------------------------------------------------------

class TestPowerOps:
    async def test_power_on_posts_correct_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctrl = RackController(host="10.0.0.5", port=8080)
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.power_on("ignored")
        assert rec.calls == [("POST", "http://10.0.0.5:8080/power/on", b"")]

    async def test_power_off_posts_correct_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctrl = RackController(host="10.0.0.5", port=8080)
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.power_off("ignored")
        assert rec.calls == [("POST", "http://10.0.0.5:8080/power/off", b"")]

    async def test_power_cycle_default_uses_pod_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default off_duration=3.0 is within the pod's built-in 2 s
        window's tolerance — exactly one POST /power/cycle."""
        ctrl = RackController(host="x", port=8080)
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.power_cycle("", off_duration=0.0)
        assert rec.calls == [("POST", "http://x:8080/power/cycle", b"")]

    async def test_power_cycle_long_duration_uses_off_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long off_duration drives off + sleep + on ourselves
        rather than the pod's fixed 2 s cycle endpoint."""
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(rack_mod.asyncio, "sleep", fake_sleep)
        ctrl = RackController(host="x", port=8080)
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.power_cycle("", off_duration=10.0)
        assert rec.calls == [
            ("POST", "http://x:8080/power/off", b""),
            ("POST", "http://x:8080/power/on", b""),
        ]
        assert sleeps == [10.0]

    async def test_close_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctrl = RackController(host="x", port=8080)
        # No urlopen patch — close() must not touch the network.
        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", _explode)
        await ctrl.close()
        await ctrl.close()

    async def test_parses_json_response_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pod returns JSON like {"camera_on": true}; we don't
        expose it but we shouldn't choke on it either."""
        body = json.dumps({"camera_on": True, "camera_path": "5V"}).encode()
        ctrl = RackController(host="x", port=8080)
        with patched_urlopen(monkeypatch, body=body):
            await ctrl.power_on("")  # no exception


# ---------------------------------------------------------------------------
# Errors surface as PowerControllerError
# ---------------------------------------------------------------------------

class TestErrorMapping:
    async def test_http_error_maps_to_power_controller_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_503(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.HTTPError(
                url=req.full_url, code=503, msg="Service Unavailable",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":"camera_path_unknown"}'),
            )

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", raise_503)
        ctrl = RackController(host="x", port=8080)
        with pytest.raises(PowerControllerError, match="rack HTTP 503"):
            await ctrl.power_on("")

    async def test_url_error_maps_to_power_controller_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_urlerr(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", raise_urlerr)
        ctrl = RackController(host="x", port=8080)
        with pytest.raises(PowerControllerError, match="rack unreachable"):
            await ctrl.power_off("")

    async def test_os_error_maps_to_power_controller_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def raise_oserr(req: Any, timeout: float | None = None) -> None:
            raise OSError("network is down")

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", raise_oserr)
        ctrl = RackController(host="x", port=8080)
        with pytest.raises(PowerControllerError, match="rack unreachable"):
            await ctrl.power_cycle("", off_duration=0.0)


def _explode(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    raise AssertionError("urlopen must not be called in this test")


# ---------------------------------------------------------------------------
# fastboot() — binary blob wire format, success/failure shapes
# ---------------------------------------------------------------------------

def _parse_fastboot_blob(blob: bytes) -> dict[str, object]:
    """Decode the binary blob the pod's /fastboot endpoint expects, so
    tests can assert on what the host packed."""
    off = 0

    def u32() -> int:
        nonlocal off
        v = int.from_bytes(blob[off:off + 4], "big")
        off += 4
        return v

    def u16() -> int:
        nonlocal off
        v = int.from_bytes(blob[off:off + 2], "big")
        off += 2
        return v

    def slice_(n: int) -> bytes:
        nonlocal off
        v = blob[off:off + n]
        off += n
        return v

    spl_address = u32()
    ddr_step_address = u32()
    uboot_address = u32()
    prestep0 = slice_(u16())
    ddrstep0 = slice_(u16())
    prestep1 = slice_(u16())
    spl = slice_(u32())
    agent = slice_(u32())
    assert off == len(blob), f"trailing bytes ({len(blob) - off}) past parsed fields"
    return {
        "spl_address": spl_address,
        "ddr_step_address": ddr_step_address,
        "uboot_address": uboot_address,
        "prestep0": prestep0,
        "ddrstep0": ddrstep0,
        "prestep1": prestep1,
        "spl": spl,
        "agent": agent,
    }


class TestFastbootWireFormat:
    """Round-trip the binary blob the pod's /fastboot endpoint expects.

    The C side in rack/firmware/main/http_api.c reads:
      [u32 spl_address][u32 ddr_step_address][u32 uboot_address]
      [u16 prestep0_len][prestep0][u16 ddrstep0_len][ddrstep0]
      [u16 prestep1_len][prestep1][u32 spl_len][spl][u32 agent_len][agent]
    all big-endian. Pin that down so a host/pod mismatch breaks loudly.
    """

    @pytest.mark.asyncio
    async def test_packs_expected_layout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        body = b'{"success":true,"last_phase":"done","elapsed_ms":4521}'

        with patched_urlopen(monkeypatch, body=body) as rec:
            await ctrl.fastboot(
                spl_address=0x04010500,
                ddr_step_address=0x04013000,
                uboot_address=0x41000000,
                prestep0=b"\x01\x02\x03\x04",
                ddrstep0=b"\x05\x06",
                prestep1=None,
                spl=b"S" * 256,
                agent=b"A" * 128,
            )

        assert len(rec.calls) == 1
        method, url, data = rec.calls[0]
        assert method == "POST"
        assert url == "http://pod:8080/fastboot"
        assert data is not None
        parsed = _parse_fastboot_blob(data)
        assert parsed["spl_address"]      == 0x04010500
        assert parsed["ddr_step_address"] == 0x04013000
        assert parsed["uboot_address"]    == 0x41000000
        assert parsed["prestep0"]         == b"\x01\x02\x03\x04"
        assert parsed["ddrstep0"]         == b"\x05\x06"
        assert parsed["prestep1"]         == b""   # None → empty
        assert parsed["spl"]              == b"S" * 256
        assert parsed["agent"]            == b"A" * 128

    @pytest.mark.asyncio
    async def test_prestep1_passthrough(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        prestep1 = bytes(range(64))
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.fastboot(
                spl_address=0, ddr_step_address=0, uboot_address=0,
                prestep0=b"", ddrstep0=b"", prestep1=prestep1,
                spl=b"", agent=b"",
            )
        parsed = _parse_fastboot_blob(rec.calls[0][2])
        assert parsed["prestep1"] == prestep1

    @pytest.mark.asyncio
    async def test_success_response_returned_verbatim(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        body = (
            b'{"success":true,"last_phase":"done",'
            b'"elapsed_ms":4521,"handshake_markers":7}'
        )
        with patched_urlopen(monkeypatch, body=body):
            result = await ctrl.fastboot(
                spl_address=0, ddr_step_address=0, uboot_address=0,
                prestep0=b"", ddrstep0=b"", prestep1=None,
                spl=b"", agent=b"",
            )
        assert result == {
            "success": True,
            "last_phase": "done",
            "elapsed_ms": 4521,
            "handshake_markers": 7,
        }

    @pytest.mark.asyncio
    async def test_pod_500_returns_json_body_not_exception(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pod returns 500 + JSON for protocol failure. The host must
        surface that JSON (so callers can read failed_phase / error)
        rather than raising — a HTTPError surfaces ONLY for non-JSON
        responses."""
        import urllib.error
        err_body = (
            b'{"success":false,"last_phase":"prestep0",'
            b'"failed_phase":"prestep0","error":"PRESTEP0 HEAD",'
            b'"elapsed_ms":214,"handshake_markers":5}'
        )

        def http500(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.HTTPError(
                url=req.full_url, code=500, msg="Internal Server Error",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(err_body),
            )

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", http500)
        ctrl = RackController(host="pod", port=8080)
        result = await ctrl.fastboot(
            spl_address=0, ddr_step_address=0, uboot_address=0,
            prestep0=b"", ddrstep0=b"", prestep1=None,
            spl=b"", agent=b"",
        )
        assert result["success"] is False
        assert result["failed_phase"] == "prestep0"
        assert "PRESTEP0" in str(result["error"])

    @pytest.mark.asyncio
    async def test_pod_unreachable_raises_power_controller_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.error

        def raise_urlerr(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", raise_urlerr)
        ctrl = RackController(host="pod", port=8080)
        with pytest.raises(PowerControllerError, match="rack unreachable"):
            await ctrl.fastboot(
                spl_address=0, ddr_step_address=0, uboot_address=0,
                prestep0=b"", ddrstep0=b"", prestep1=None,
                spl=b"", agent=b"",
            )

    @pytest.mark.asyncio
    async def test_realistic_blob_size_within_pod_limit(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pod caps body at 1 MiB (FASTBOOT_MAX_BODY).  A typical
        upload is profile (~140 B) + SPL (~24 KB) + agent (~17 KB)
        ≈ 41 KB. Make sure our packing matches that ballpark."""
        ctrl = RackController(host="pod", port=8080)
        prestep = b"\xab" * 64
        ddr = b"\xcd" * 64
        spl = b"\x90" * 24_576
        agent = b"\x55" * 17_104
        with patched_urlopen(monkeypatch) as rec:
            await ctrl.fastboot(
                spl_address=0x04010500,
                ddr_step_address=0x04013000,
                uboot_address=0x41000000,
                prestep0=prestep, ddrstep0=ddr, prestep1=None,
                spl=spl, agent=agent,
            )
        # 3*u32 + 3*u16 + 64 + 64 + 0 + u32 + 24576 + u32 + 17104
        # = 12 + 6 + 128 + 4 + 24576 + 4 + 17104 = 41834
        assert len(rec.calls[0][2]) == 41834
        assert len(rec.calls[0][2]) < 1024 * 1024  # < FASTBOOT_MAX_BODY


# ---------------------------------------------------------------------------
# tftp_put / tftp_delete / tftp_clear / tftp_list — pod-hosted TFTP staging
# ---------------------------------------------------------------------------

class TestTftpStaging:
    """RackController.tftp_put / delete / clear / list — the host wrapper
    around the pod's POST /tftp/<name>, DELETE /tftp/<name>, GET /tftp.

    Lets defib (or any caller) stage firmware into the pod's PSRAM so the
    camera's U-Boot can fetch it from 192.168.1.1 over the local LAN —
    no host-side TFTP server required."""

    @pytest.mark.asyncio
    async def test_tftp_put_posts_correct_url_and_body(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        body = b'{"name":"uImage","size":2048}'
        with patched_urlopen(monkeypatch, body=body) as rec:
            r = await ctrl.tftp_put("uImage", b"X" * 2048)
        assert len(rec.calls) == 1
        method, url, data = rec.calls[0]
        assert method == "POST"
        assert url == "http://pod:8080/tftp/uImage"
        assert data == b"X" * 2048
        assert r == {"name": "uImage", "size": 2048}

    @pytest.mark.asyncio
    async def test_tftp_put_rejects_path_traversal(self) -> None:
        ctrl = RackController(host="pod", port=8080)
        with pytest.raises(PowerControllerError, match="bad TFTP filename"):
            await ctrl.tftp_put("../etc/passwd", b"x")
        with pytest.raises(PowerControllerError, match="bad TFTP filename"):
            await ctrl.tftp_put("sub/path", b"x")
        with pytest.raises(PowerControllerError, match="bad TFTP filename"):
            await ctrl.tftp_put("", b"x")

    @pytest.mark.asyncio
    async def test_tftp_delete_one(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        with patched_urlopen(monkeypatch, body=b'{"ok":true}') as rec:
            await ctrl.tftp_delete("uImage")
        method, url, _ = rec.calls[0]
        assert method == "DELETE"
        assert url == "http://pod:8080/tftp/uImage"

    @pytest.mark.asyncio
    async def test_tftp_clear_all(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        with patched_urlopen(monkeypatch, body=b'{"ok":true}') as rec:
            await ctrl.tftp_clear()
        method, url, _ = rec.calls[0]
        assert method == "DELETE"
        assert url == "http://pod:8080/tftp"   # no trailing /<name>

    @pytest.mark.asyncio
    async def test_tftp_list_returns_dict(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = RackController(host="pod", port=8080)
        body = (
            b'{"files":[{"name":"uImage","size":2048,"reads":0}],'
            b'"max_size_bytes":8388608,"max_slots":4,'
            b'"psram_free_bytes":6291456,"psram_largest_free_block":4194304}'
        )
        with patched_urlopen(monkeypatch, body=body) as rec:
            r = await ctrl.tftp_list()
        method, url, _ = rec.calls[0]
        assert method == "GET"
        assert url == "http://pod:8080/tftp"
        assert r["files"][0]["name"] == "uImage"
        assert r["max_size_bytes"] == 8388608

    @pytest.mark.asyncio
    async def test_tftp_put_oom_returns_typed_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pod returns 503 + JSON body when PSRAM allocation fails.
        Surface that as a PowerControllerError with the body so callers
        can fall back to host TFTP or chunked upload."""
        import urllib.error
        err_body = (
            b'{"error":"oom","requested":8388608,'
            b'"psram_largest_free":4194304}'
        )

        def http503(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.HTTPError(
                url=req.full_url, code=503, msg="Service Unavailable",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(err_body),
            )

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", http503)
        ctrl = RackController(host="pod", port=8080)
        with pytest.raises(PowerControllerError, match="503"):
            await ctrl.tftp_put("rootfs", b"X" * 4096)
