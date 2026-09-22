from __future__ import annotations

import pytest

from defib.flashdump import send_command
from defib.transport.base import TransportTimeout
from defib.transport.mock import MockTransport


class EchoingPromptTransport(MockTransport):
    """Echo input bytes, then return a U-Boot prompt when Enter is sent."""

    def __init__(self, *, corrupt_once_at: int | None = None) -> None:
        super().__init__(flush_clears_buffer=False)
        self.corrupt_once_at = corrupt_once_at
        self._position = 0
        self._corrupted = False

    async def write(self, data: bytes) -> None:
        await super().write(data)
        for byte in data:
            if byte == 0x03:
                # Ctrl-C used by the retry path; no echo is required.
                continue
            if byte == 0x0D:
                self.enqueue_rx(b"\r\nOpenIPC # ")
                self._position = 0
                continue
            if (
                self.corrupt_once_at is not None
                and not self._corrupted
                and self._position == self.corrupt_once_at
            ):
                self.enqueue_rx(b"?")
                self._corrupted = True
            else:
                self.enqueue_rx(bytes((byte,)))
            self._position += 1


@pytest.mark.asyncio
async def test_echo_verified_command_executes_only_after_full_match() -> None:
    transport = EchoingPromptTransport()
    command = "sf erase 0x50000 0x300000"

    response = await send_command(
        transport, command, timeout=1.0, wait_for="# ", verify_echo=True
    )

    assert "OpenIPC # " in response
    assert transport.all_tx_data.endswith(command.encode("ascii") + b"\r")


@pytest.mark.asyncio
async def test_echo_mismatch_cancels_line_and_retypes_before_enter() -> None:
    transport = EchoingPromptTransport(corrupt_once_at=5)
    command = "sf write 0x82000000 0x50000 0x1c6f58"

    response = await send_command(
        transport, command, timeout=1.0, wait_for="# ", verify_echo=True
    )

    assert "OpenIPC # " in response
    # The first corrupted attempt must be cancelled before any Enter executes it.
    first_cr = transport.all_tx_data.find(b"\r")
    first_ctrl_c = transport.all_tx_data.find(b"\x03")
    assert first_ctrl_c != -1
    assert first_ctrl_c < first_cr
    assert transport.all_tx_data.endswith(command.encode("ascii") + b"\r")


class NoEchoPromptTransport(MockTransport):
    async def write(self, data: bytes) -> None:
        await super().write(data)
        if b"\r" in data:
            self.enqueue_rx(b"\r\nOpenIPC # ")


@pytest.mark.asyncio
async def test_echo_verification_refuses_unacknowledged_command() -> None:
    transport = NoEchoPromptTransport(flush_clears_buffer=False)
    with pytest.raises(TransportTimeout, match="echo verification failed"):
        await send_command(
            transport,
            "sf erase 0 0x40000",
            timeout=0.1,
            wait_for="# ",
            verify_echo=True,
        )



class PartialResponseNoPromptTransport(MockTransport):
    async def write(self, data: bytes) -> None:
        await super().write(data)
        if b"\r" in data:
            self.enqueue_rx(b"Bytes transferred = 1558411\n")


@pytest.mark.asyncio
async def test_wait_for_prompt_rejects_partial_command_response() -> None:
    transport = PartialResponseNoPromptTransport(flush_clears_buffer=False)

    with pytest.raises(TransportTimeout, match="waiting for '# '"):
        await send_command(
            transport,
            "tftpboot 0x82000000 k",
            timeout=0.05,
            wait_for="# ",
            require_prompt=True,
        )


@pytest.mark.asyncio
async def test_wait_for_prompt_keeps_legacy_partial_response_when_not_required() -> None:
    transport = PartialResponseNoPromptTransport(flush_clears_buffer=False)

    response = await send_command(
        transport,
        "tftpboot 0x82000000 k",
        timeout=0.05,
        wait_for="# ",
    )

    assert "Bytes transferred = 1558411" in response



class SilentAfterCommandTransport(MockTransport):
    async def write(self, data: bytes) -> None:
        await super().write(data)


@pytest.mark.asyncio
async def test_legacy_wait_for_allows_reset_without_returned_prompt() -> None:
    transport = SilentAfterCommandTransport(flush_clears_buffer=False)

    response = await send_command(
        transport,
        "reset",
        timeout=0.05,
        wait_for="# ",
    )

    assert response == ""
