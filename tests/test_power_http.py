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

    def fake(req: Any, timeout: float | None = None) -> FakeResponse:
        seen.append(req.full_url)
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


class TestRedaction:
    SECRET_ON = "http://admin:hunter2@relay.local:8080/relay/on?token=s3cret"

    async def test_logs_hide_credentials(
        self, calls: list[str], caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.INFO, logger="defib.power.http")
        await HttpRelayController(on_url=self.SECRET_ON, off_url=OFF).power_on("")
        # The query still goes out; the user info moves into a header.
        assert calls == ["http://relay.local:8080/relay/on?token=s3cret"]
        assert "hunter2" not in caplog.text
        assert "s3cret" not in caplog.text
        assert "http://relay.local:8080/relay/on?..." in caplog.text

    async def test_errors_hide_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.HTTPError(
                url.full_url, 401, "Unauthorized", {}, io.BytesIO(b"bad token s3cret"),  # type: ignore[attr-defined, arg-type]
            )

        monkeypatch.setattr(http_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError) as exc:
            await HttpRelayController(on_url=self.SECRET_ON, off_url=OFF).power_on("")
        assert "hunter2" not in str(exc.value)
        assert "s3cret" not in str(exc.value)
        assert "401" in str(exc.value)

    async def test_unreachable_message_hides_credentials(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail(url: str, timeout: float | None = None) -> Any:  # noqa: ANN401
            raise urllib.error.URLError(f"cannot reach {url}")

        monkeypatch.setattr(http_mod.urllib.request, "urlopen", fail)
        with pytest.raises(PowerControllerError) as exc:
            await HttpRelayController(on_url=self.SECRET_ON, off_url=OFF).power_on("")
        assert "hunter2" not in str(exc.value)
        assert "s3cret" not in str(exc.value)


class TestBasicAuth:
    async def test_userinfo_becomes_basic_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import base64

        seen: list[Any] = []

        def fake(req: Any, timeout: float | None = None) -> FakeResponse:
            seen.append(req)
            return FakeResponse(b"ok")

        monkeypatch.setattr(http_mod.urllib.request, "urlopen", fake)
        url = "http://admin:p%40ss@relay.local/relay/off"
        await HttpRelayController(on_url=ON, off_url=url).power_off("")
        req = seen[0]
        assert req.full_url == "http://relay.local/relay/off"
        expected = base64.b64encode(b"admin:p@ss").decode()
        assert req.get_header("Authorization") == f"Basic {expected}"
