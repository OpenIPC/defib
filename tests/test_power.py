"""Tests for power controller ABC, RouterOS wire protocol, and auto-discovery."""

from __future__ import annotations

import asyncio
import struct

import pytest

from defib.power.base import PowerController, PowerControllerError
from defib.power.routeros import (
    RouterOSController,
    _encode_length,
    _encode_word,
    _read_length,
    _read_sentence,
    _read_word,
    _parse_items,
)


# ---------------------------------------------------------------------------
# PowerController ABC
# ---------------------------------------------------------------------------

class MockPowerController(PowerController):
    def __init__(self) -> None:
        self.actions: list[tuple[str, str]] = []

    @classmethod
    def name(cls) -> str:
        return "Mock"

    async def power_off(self, port: str) -> None:
        self.actions.append(("off", port))

    async def power_on(self, port: str) -> None:
        self.actions.append(("on", port))

    async def close(self) -> None:
        pass


class TestPowerControllerABC:
    async def test_power_cycle_calls_off_then_on(self) -> None:
        ctrl = MockPowerController()
        await ctrl.power_cycle("ether3", off_duration=0.01)
        assert ctrl.actions == [("off", "ether3"), ("on", "ether3")]

    async def test_power_cycle_default_interface(self) -> None:
        ctrl = MockPowerController()
        await ctrl.power_cycle("ether5", off_duration=0.01)
        assert ctrl.actions[0] == ("off", "ether5")
        assert ctrl.actions[1] == ("on", "ether5")

    async def test_name(self) -> None:
        assert MockPowerController.name() == "Mock"


# ---------------------------------------------------------------------------
# RouterOS wire protocol encoding
# ---------------------------------------------------------------------------

class TestRouterOSEncoding:
    def test_encode_length_short(self) -> None:
        assert _encode_length(0) == b"\x00"
        assert _encode_length(1) == b"\x01"
        assert _encode_length(0x7F) == b"\x7f"

    def test_encode_length_two_bytes(self) -> None:
        result = _encode_length(0x80)
        assert len(result) == 2
        assert result == struct.pack("!H", 0x80 | 0x8000)

    def test_encode_length_two_bytes_max(self) -> None:
        result = _encode_length(0x3FFF)
        assert len(result) == 2

    def test_encode_length_three_bytes(self) -> None:
        result = _encode_length(0x4000)
        assert len(result) == 3

    def test_encode_length_four_bytes(self) -> None:
        result = _encode_length(0x200000)
        assert len(result) == 4

    def test_encode_length_too_long(self) -> None:
        with pytest.raises(PowerControllerError, match="Word too long"):
            _encode_length(0x10000000)

    def test_encode_word_roundtrip(self) -> None:
        word = "/interface/ethernet/poe/print"
        encoded = _encode_word(word)
        assert encoded[0] == len(word)
        assert encoded[1:] == word.encode("utf-8")

    async def test_read_length_short(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x05")
        reader.feed_eof()
        assert await _read_length(reader) == 5

    async def test_read_length_two_bytes(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!H", 0x80 | 0x8000))
        reader.feed_eof()
        assert await _read_length(reader) == 0x80

    async def test_read_word(self) -> None:
        reader = asyncio.StreamReader()
        word = "hello"
        reader.feed_data(_encode_word(word))
        reader.feed_eof()
        assert await _read_word(reader) == "hello"

    async def test_read_sentence(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(_encode_word("!done"))
        reader.feed_data(b"\x00")  # end of sentence
        reader.feed_eof()
        sentence = await _read_sentence(reader)
        assert sentence == ["!done"]

    async def test_read_sentence_multiple_words(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(_encode_word("!re"))
        reader.feed_data(_encode_word("=name=ether3"))
        reader.feed_data(_encode_word("=comment=IVGHP203Y-AF 00:12:31:5e:e0:d2"))
        reader.feed_data(b"\x00")
        reader.feed_eof()
        sentence = await _read_sentence(reader)
        assert sentence[0] == "!re"
        assert "=name=ether3" in sentence
        assert "=comment=IVGHP203Y-AF 00:12:31:5e:e0:d2" in sentence


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestParseItems:
    def test_parse_re_sentences(self) -> None:
        response = [
            ["!re", "=.id=*1", "=name=ether1", "=poe-out=auto-on", "=comment="],
            ["!re", "=.id=*3", "=name=ether3", "=poe-out=auto-on",
             "=comment=IVGHP203Y-AF 00:12:31:5e:e0:d2"],
            ["!done"],
        ]
        items = _parse_items(response)
        assert len(items) == 2
        assert items[0]["name"] == "ether1"
        assert items[1]["name"] == "ether3"
        assert items[1]["comment"] == "IVGHP203Y-AF 00:12:31:5e:e0:d2"

    def test_parse_empty(self) -> None:
        assert _parse_items([["!done"]]) == []


# ---------------------------------------------------------------------------
# RouterOSController.from_env
# ---------------------------------------------------------------------------

class TestRouterOSFromEnv:
    def test_missing_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFIB_POE_HOST", raising=False)
        with pytest.raises(PowerControllerError, match="DEFIB_POE_HOST"):
            RouterOSController.from_env()

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_POE_HOST", "10.0.0.1")
        monkeypatch.delenv("DEFIB_POE_USER", raising=False)
        monkeypatch.delenv("DEFIB_POE_PASS", raising=False)
        monkeypatch.delenv("DEFIB_POE_API_PORT", raising=False)
        ctrl = RouterOSController.from_env()
        assert ctrl._host == "10.0.0.1"
        assert ctrl._username == "admin"
        assert ctrl._password == ""
        assert ctrl._api_port == 8728

    def test_custom_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFIB_POE_HOST", "192.168.88.1")
        monkeypatch.setenv("DEFIB_POE_USER", "root")
        monkeypatch.setenv("DEFIB_POE_PASS", "secret")
        monkeypatch.setenv("DEFIB_POE_API_PORT", "9999")
        ctrl = RouterOSController.from_env()
        assert ctrl._host == "192.168.88.1"
        assert ctrl._username == "root"
        assert ctrl._password == "secret"
        assert ctrl._api_port == 9999
