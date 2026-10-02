"""Tests for the generic HTTP relay power controller."""

from __future__ import annotations

import io
import urllib.error
from typing import Any

import pytest

from defib.power import http as http_mod
from defib.power.base import PowerControllerError
from defib.power.factory import power_controller_from_env
from defib.power.http import HttpRelayController

ON = "http://127.0.0.1:8090/relay/on"
OFF = "http://127.0.0.1:8090/relay/off"


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(url: str, timeout: float | None = None) -> FakeResponse:
        seen.append(url)
        return FakeResponse(b'{"state":"on"}')

    monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake)
    return seen


class TestFromEnv:
    @pytest.mark.parametrize("missing", ["DEFIB_HTTP_POWER_ON_URL", "DEFIB_HTTP_POWER_OFF_URL"])
    def test_missing_url(self, monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
        monkeypatch.setenv("DEFIB_HTTP_POWER_ON_URL", ON)
        monkeypatch.setenv("DEFIB_HTTP_POWER_OFF_URL", OFF)
        monkeypatch.delenv(missing)
        with pytest.raises(PowerControllerError, match="DEFIB_HTTP_POWER_ON_URL"):
            HttpRelayController.from_env()

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_HTTP_POWER_ON_URL", ON)
        monkeypatch.setenv("DEFIB_HTTP_POWER_OFF_URL", OFF)
        monkeypatch.setenv("DEFIB_HTTP_POWER_TIMEOUT", "2.5")
        assert HttpRelayController.from_env()._timeout == 2.5
        monkeypatch.setenv("DEFIB_HTTP_POWER_TIMEOUT", "soon")
        with pytest.raises(PowerControllerError, match="TIMEOUT"):
            HttpRelayController.from_env()

    def test_factory_dispatches_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_POWER_TYPE", "http")
        monkeypatch.setenv("DEFIB_HTTP_POWER_ON_URL", ON)
        monkeypatch.setenv("DEFIB_HTTP_POWER_OFF_URL", OFF)
        monkeypatch.delenv("DEFIB_HTTP_POWER_TIMEOUT", raising=False)
        assert isinstance(power_controller_from_env(), HttpRelayController)


class TestPowerOps:
    async def test_on_off(self, calls: list[str]) -> None:
        ctrl = HttpRelayController(on_url=ON, off_url=OFF)
        await ctrl.power_off("ignored")
        await ctrl.power_on("ignored")
        assert calls == [OFF, ON]

    async def test_cycle_is_host_timed(
        self, calls: list[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("defib.power.base.asyncio.sleep", fake_sleep)
        ctrl = HttpRelayController(on_url=ON, off_url=OFF)
        await ctrl.power_cycle("", off_duration=2.0)
        assert calls == [OFF, ON]
        assert sleeps == [2.0]

    async def test_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"cloud down"))  # type: ignore[arg-type]

        monkeypatch.setattr(http_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError, match="502.*cloud down"):
            await HttpRelayController(on_url=ON, off_url=OFF).power_on("")

    async def test_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(http_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError, match="unreachable"):
            await HttpRelayController(on_url=ON, off_url=OFF).power_off("")
