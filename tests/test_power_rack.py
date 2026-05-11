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
