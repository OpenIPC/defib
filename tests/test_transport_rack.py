"""Tests for RackTransport — TCP UART bridge + HTTP /uart/baud."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from defib.transport import rack as rack_mod
from defib.transport.base import TransportError
from defib.transport.rack import RackTransport


class _FakeResp(io.BytesIO):
    """Context-manager wrapper for urlopen() mock."""

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _Recorder:
    def __init__(self, body: bytes = b"{}") -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.body = body

    def __call__(self, req: Any, timeout: float | None = None) -> _FakeResp:
        self.calls.append((req.get_method(), req.full_url, req.data))
        return _FakeResp(self.body)


class TestSetBaudrate:
    @pytest.mark.asyncio
    async def test_posts_uart_baud_with_correct_url_and_body(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Don't open a real socket — just exercise the override.
        rt = RackTransport.__new__(RackTransport)
        rt._http_base = "http://10.0.0.5:8080"  # type: ignore[attr-defined]

        rec = _Recorder()
        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", rec)

        await rt.set_baudrate(921600)

        assert len(rec.calls) == 1
        method, url, body = rec.calls[0]
        assert method == "POST"
        assert url == "http://10.0.0.5:8080/uart/baud"
        assert json.loads(body) == {"rate": 921600}

    @pytest.mark.asyncio
    async def test_unreachable_pod_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.error

        def boom(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", boom)
        rt = RackTransport.__new__(RackTransport)
        rt._http_base = "http://x:8080"  # type: ignore[attr-defined]
        with pytest.raises(TransportError, match="rack unreachable"):
            await rt.set_baudrate(921600)

    @pytest.mark.asyncio
    async def test_http_error_raises_transport_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.error

        def http503(req: Any, timeout: float | None = None) -> None:
            raise urllib.error.HTTPError(
                url=req.full_url, code=503, msg="Service Unavailable",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"error":"uart_set_baud_failed"}'),
            )

        monkeypatch.setattr(rack_mod.urllib.request, "urlopen", http503)
        rt = RackTransport.__new__(RackTransport)
        rt._http_base = "http://x:8080"  # type: ignore[attr-defined]
        with pytest.raises(TransportError, match="rack HTTP 503"):
            await rt.set_baudrate(921600)


class TestSerialPlatformURLScheme:
    """`rack://` URL routing in defib.transport.serial_platform."""

    @pytest.mark.asyncio
    async def test_parses_default_ports(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_create(
            host: str, bridge_port: int = 9000, http_port: int = 8080,
        ) -> object:
            captured.update(host=host, bridge_port=bridge_port, http_port=http_port)
            return object()

        monkeypatch.setattr(RackTransport, "create_rack", fake_create)
        from defib.transport.serial_platform import create_transport

        await create_transport("rack://10.0.0.5")
        assert captured == {"host": "10.0.0.5", "bridge_port": 9000, "http_port": 8080}

    @pytest.mark.asyncio
    async def test_parses_custom_bridge_port(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_create(host: str, bridge_port: int = 9000, http_port: int = 8080) -> object:
            captured.update(host=host, bridge_port=bridge_port, http_port=http_port)
            return object()

        monkeypatch.setattr(RackTransport, "create_rack", fake_create)
        from defib.transport.serial_platform import create_transport

        await create_transport("rack://pod.local:9001")
        assert captured["host"] == "pod.local"
        assert captured["bridge_port"] == 9001

    @pytest.mark.asyncio
    async def test_parses_api_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_create(host: str, bridge_port: int = 9000, http_port: int = 8080) -> object:
            captured.update(host=host, bridge_port=bridge_port, http_port=http_port)
            return object()

        monkeypatch.setattr(RackTransport, "create_rack", fake_create)
        from defib.transport.serial_platform import create_transport

        await create_transport("rack://10.0.0.5:9000?api=8088")
        assert captured["http_port"] == 8088

    @pytest.mark.asyncio
    async def test_rejects_missing_host(self) -> None:
        from defib.transport.serial_platform import create_transport

        with pytest.raises(TransportError, match="needs a host"):
            await create_transport("rack://")
