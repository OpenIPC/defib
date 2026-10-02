"""Tests for the Tasmota smart-plug power controller."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from typing import Any

import pytest

from defib.power import tasmota as tasmota_mod
from defib.power.base import PowerControllerError
from defib.power.factory import power_controller_from_env
from defib.power.tasmota import TasmotaController


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakePlug:
    """Answers /cm?cmnd= like a single-relay Tasmota plug."""

    def __init__(self, relay_key: str = "POWER") -> None:
        self.cmnds: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        self.state = "ON"
        self.relay_key = relay_key
        self.reply_override: bytes | None = None

    def __call__(self, url: str, timeout: float | None = None) -> FakeResponse:
        parsed = urllib.parse.urlparse(url)
        assert parsed.path == "/cm"
        query = urllib.parse.parse_qs(parsed.query)
        self.queries.append(query)
        cmnd = query["cmnd"][0]
        self.cmnds.append(cmnd)
        if self.reply_override is not None:
            return FakeResponse(self.reply_override)
        parts = cmnd.split()
        if len(parts) == 2:
            self.state = parts[1]
        return FakeResponse(json.dumps({self.relay_key: self.state}).encode())


@pytest.fixture
def plug(monkeypatch: pytest.MonkeyPatch) -> FakePlug:
    fake = FakePlug()
    monkeypatch.setattr(tasmota_mod.urllib.request, "urlopen", fake)
    return fake


class TestFromEnv:
    def test_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFIB_TASMOTA_HOST", raising=False)
        with pytest.raises(PowerControllerError, match="DEFIB_TASMOTA_HOST"):
            TasmotaController.from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_TASMOTA_HOST", "10.216.128.80")
        for var in ("DEFIB_TASMOTA_RELAY", "DEFIB_TASMOTA_USER", "DEFIB_TASMOTA_PASSWORD"):
            monkeypatch.delenv(var, raising=False)
        ctrl = TasmotaController.from_env()
        assert ctrl._host == "10.216.128.80"
        assert ctrl._relay is None
        assert ctrl._user is None

    def test_bad_relay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_TASMOTA_HOST", "x")
        monkeypatch.setenv("DEFIB_TASMOTA_RELAY", "two")
        with pytest.raises(PowerControllerError, match="DEFIB_TASMOTA_RELAY"):
            TasmotaController.from_env()

    def test_factory_dispatches_tasmota(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_POWER_TYPE", "tasmota")
        monkeypatch.setenv("DEFIB_TASMOTA_HOST", "10.0.0.5")
        assert isinstance(power_controller_from_env(), TasmotaController)


class TestPowerOps:
    async def test_power_on_off(self, plug: FakePlug) -> None:
        ctrl = TasmotaController(host="10.0.0.5")
        await ctrl.power_off("ignored")
        await ctrl.power_on("ignored")
        assert plug.cmnds == ["Power OFF", "Power ON"]

    async def test_relay_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakePlug(relay_key="POWER2")
        monkeypatch.setattr(tasmota_mod.urllib.request, "urlopen", fake)
        ctrl = TasmotaController(host="x", relay=2)
        await ctrl.power_off("")
        assert fake.cmnds == ["Power2 OFF"]

    async def test_credentials_in_query(self, plug: FakePlug) -> None:
        ctrl = TasmotaController(host="x", user="admin", password="s3cret")
        await ctrl.power_on("")
        assert plug.queries[0]["user"] == ["admin"]
        assert plug.queries[0]["password"] == ["s3cret"]

    async def test_state_mismatch_raises(self, plug: FakePlug) -> None:
        plug.reply_override = b'{"POWER":"ON"}'
        ctrl = TasmotaController(host="x")
        with pytest.raises(PowerControllerError, match="asked for OFF"):
            await ctrl.power_off("")

    async def test_auth_warning_raises(self, plug: FakePlug) -> None:
        plug.reply_override = b'{"WARNING":"Need user=<username>&password=<password>"}'
        ctrl = TasmotaController(host="x")
        with pytest.raises(PowerControllerError, match="Need user"):
            await ctrl.power_on("")

    async def test_non_json_raises(self, plug: FakePlug) -> None:
        plug.reply_override = b"<html>"
        ctrl = TasmotaController(host="x")
        with pytest.raises(PowerControllerError, match="non-JSON"):
            await ctrl.power_on("")


class TestPowerCycle:
    async def test_cycle_is_verified_off_then_on(
        self, plug: FakePlug, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("defib.power.base.asyncio.sleep", fake_sleep)
        await TasmotaController(host="x").power_cycle("", off_duration=3.0)
        # No Backlog: commands sent during its Delay cut the delay short.
        assert plug.cmnds == ["Power OFF", "Power ON"]
        assert sleeps == [3.0]

    async def test_cycle_fails_if_plug_does_not_switch_off(
        self, plug: FakePlug, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr("defib.power.base.asyncio.sleep", fake_sleep)
        plug.reply_override = b'{"POWER":"ON"}'
        with pytest.raises(PowerControllerError, match="asked for OFF"):
            await TasmotaController(host="x").power_cycle("", off_duration=3.0)


class TestErrorMapping:
    async def test_url_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(tasmota_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError, match="unreachable"):
            await TasmotaController(host="x").power_on("")

    async def test_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO())  # type: ignore[arg-type]

        monkeypatch.setattr(tasmota_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError, match="HTTP 401"):
            await TasmotaController(host="x").power_on("")
