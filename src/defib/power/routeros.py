"""MikroTik RouterOS PoE power controller.

Implements a minimal RouterOS API client (word-length encoding, login,
command execution) over async TCP — no external dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import struct

from defib.power.base import PowerController, PowerControllerError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RouterOS API wire protocol helpers
# ---------------------------------------------------------------------------

def _encode_length(length: int) -> bytes:
    """Encode a word length in RouterOS API wire format."""
    if length < 0x80:
        return struct.pack("!B", length)
    if length < 0x4000:
        return struct.pack("!H", length | 0x8000)
    if length < 0x200000:
        b = length | 0xC00000
        return struct.pack("!BH", (b >> 16) & 0xFF, b & 0xFFFF)
    if length < 0x10000000:
        return struct.pack("!I", length | 0xE0000000)
    raise PowerControllerError(f"Word too long: {length}")


async def _read_length(reader: asyncio.StreamReader) -> int:
    """Read a word length from the stream."""
    b = (await reader.readexactly(1))[0]
    if b < 0x80:
        return b
    if b < 0xC0:
        b2 = (await reader.readexactly(1))[0]
        return ((b & 0x3F) << 8) | b2
    if b < 0xE0:
        rest = await reader.readexactly(2)
        return ((b & 0x1F) << 16) | (rest[0] << 8) | rest[1]
    if b < 0xF0:
        rest = await reader.readexactly(3)
        return ((b & 0x0F) << 24) | (rest[0] << 16) | (rest[1] << 8) | rest[2]
    raise PowerControllerError(f"Unsupported length encoding: 0x{b:02x}")


async def _read_word(reader: asyncio.StreamReader) -> str:
    """Read one API word (length-prefixed UTF-8 string)."""
    length = await _read_length(reader)
    if length == 0:
        return ""
    data = await reader.readexactly(length)
    return data.decode("utf-8", errors="replace")


def _encode_word(word: str) -> bytes:
    """Encode one API word (length prefix + UTF-8 payload)."""
    payload = word.encode("utf-8")
    return _encode_length(len(payload)) + payload


async def _send_sentence(
    writer: asyncio.StreamWriter, words: list[str]
) -> None:
    """Send a sentence (list of words terminated by empty word)."""
    for word in words:
        writer.write(_encode_word(word))
    writer.write(b"\x00")  # empty word = end of sentence
    await writer.drain()


async def _read_sentence(reader: asyncio.StreamReader) -> list[str]:
    """Read a complete sentence (words until empty word)."""
    words: list[str] = []
    while True:
        word = await _read_word(reader)
        if word == "":
            break
        words.append(word)
    return words


# ---------------------------------------------------------------------------
# RouterOS API client
# ---------------------------------------------------------------------------

class _RouterOSConnection:
    """Minimal async RouterOS API connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, words: list[str]) -> None:
        await _send_sentence(self._writer, words)

    async def read_response(self) -> list[list[str]]:
        """Read sentences until !done or !trap."""
        sentences: list[list[str]] = []
        while True:
            sentence = await _read_sentence(self._reader)
            if not sentence:
                continue
            sentences.append(sentence)
            if sentence[0] in ("!done", "!trap"):
                break
        return sentences

    async def call(self, *words: str) -> list[list[str]]:
        """Send a command and return the full response."""
        await self.send(list(words))
        return await self.read_response()

    async def login(self, username: str, password: str) -> None:
        """Authenticate with the RouterOS device.

        Supports both the legacy (pre-6.43 challenge-response) and modern
        (post-6.43 plaintext) login methods.
        """
        response = await self.call("/login", f"=name={username}", f"=password={password}")
        # Check for !trap (auth failure)
        for sentence in response:
            if sentence[0] == "!trap":
                msg = _extract_attr(sentence, "message") or "authentication failed"
                raise PowerControllerError(f"RouterOS login failed: {msg}")

        # Legacy login: !done with =ret= challenge hash
        for sentence in response:
            if sentence[0] == "!done":
                challenge = _extract_attr(sentence, "ret")
                if challenge:
                    await self._login_legacy(username, password, challenge)
                return
        raise PowerControllerError("Unexpected login response")

    async def _login_legacy(
        self, username: str, password: str, challenge_hex: str
    ) -> None:
        """Legacy challenge-response authentication (RouterOS < 6.43)."""
        challenge = bytes.fromhex(challenge_hex)
        md5 = hashlib.md5()
        md5.update(b"\x00")
        md5.update(password.encode("utf-8"))
        md5.update(challenge)
        hashed = "00" + md5.hexdigest()
        response = await self.call(
            "/login", f"=name={username}", f"=response={hashed}"
        )
        for sentence in response:
            if sentence[0] == "!trap":
                msg = _extract_attr(sentence, "message") or "authentication failed"
                raise PowerControllerError(f"RouterOS login failed: {msg}")

    def close(self) -> None:
        self._writer.close()


def _extract_attr(sentence: list[str], key: str) -> str | None:
    """Extract =key=value from a sentence."""
    prefix = f"={key}="
    for word in sentence:
        if word.startswith(prefix):
            return word[len(prefix):]
    return None


def _parse_items(response: list[list[str]]) -> list[dict[str, str]]:
    """Parse !re sentences into list of dicts."""
    items: list[dict[str, str]] = []
    for sentence in response:
        if sentence[0] != "!re":
            continue
        item: dict[str, str] = {}
        for word in sentence[1:]:
            if word.startswith("=") and "=" in word[1:]:
                key, _, value = word[1:].partition("=")
                item[key] = value
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# RouterOS PoE controller
# ---------------------------------------------------------------------------

class RouterOSController(PowerController):
    """Controls PoE ports on MikroTik switches via RouterOS API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        api_port: int = 8728,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._api_port = api_port
        self._conn: _RouterOSConnection | None = None
        self._saved_poe_out: dict[str, str] = {}

    @classmethod
    def name(cls) -> str:
        return "MikroTik RouterOS PoE"

    @classmethod
    def from_env(cls) -> RouterOSController:
        """Create from DEFIB_POE_* environment variables.

        Required:
            DEFIB_POE_HOST: RouterOS switch IP/hostname
        Optional:
            DEFIB_POE_USER: Username (default: admin)
            DEFIB_POE_PASS: Password (default: empty)
            DEFIB_POE_API_PORT: API port (default: 8728)
        """
        host = os.environ.get("DEFIB_POE_HOST")
        if not host:
            raise PowerControllerError(
                "DEFIB_POE_HOST env var required for RouterOS power control"
            )
        return cls(
            host=host,
            username=os.environ.get("DEFIB_POE_USER", "admin"),
            password=os.environ.get("DEFIB_POE_PASS", ""),
            api_port=int(os.environ.get("DEFIB_POE_API_PORT", "8728")),
        )

    async def _connect(self) -> _RouterOSConnection:
        """Lazy connect and authenticate."""
        if self._conn is not None:
            return self._conn
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._api_port),
                timeout=10.0,
            )
        except (OSError, asyncio.TimeoutError) as e:
            raise PowerControllerError(
                f"Cannot connect to RouterOS at {self._host}:{self._api_port}: {e}"
            ) from e
        conn = _RouterOSConnection(reader, writer)
        await conn.login(self._username, self._password)
        self._conn = conn
        logger.info("Connected to RouterOS at %s:%d", self._host, self._api_port)
        return conn

    async def find_port_by_comment(self, search: str) -> str:
        """Find an ethernet interface whose comment contains `search`.

        Queries /interface/ethernet/print (where comments are stored)
        and matches the search string case-insensitively.

        Returns the interface name (e.g. "ether3").

        Raises PowerControllerError if no match is found.
        """
        conn = await self._connect()
        response = await conn.call("/interface/ethernet/print")
        items = _parse_items(response)

        search_lower = search.lower()
        for item in items:
            comment = item.get("comment", "")
            if search_lower in comment.lower():
                iface_name = item.get("name", "")
                logger.info(
                    "Matched '%s' -> interface %s (comment: %s)",
                    search, iface_name, comment,
                )
                return iface_name

        available = [
            f"  {item.get('name', '?')}: {item.get('comment', '(no comment)')}"
            for item in items
            if item.get("comment")
        ]
        raise PowerControllerError(
            f"No ethernet interface with comment matching '{search}'.\n"
            f"Interfaces with comments:\n" + "\n".join(available)
        )

    async def _set_poe(self, interface_name: str, poe_out: str) -> None:
        """Set poe-out on a specific ethernet interface."""
        conn = await self._connect()
        # First find the .id for this interface name
        response = await conn.call(
            "/interface/ethernet/poe/print",
            f"?name={interface_name}",
        )
        items = _parse_items(response)
        if not items:
            raise PowerControllerError(
                f"PoE interface '{interface_name}' not found on switch"
            )
        item_id = items[0].get(".id")
        if not item_id:
            raise PowerControllerError(
                f"No .id for interface '{interface_name}'"
            )

        response = await conn.call(
            "/interface/ethernet/poe/set",
            f"=.id={item_id}",
            f"=poe-out={poe_out}",
        )
        for sentence in response:
            if sentence[0] == "!trap":
                msg = _extract_attr(sentence, "message") or "unknown error"
                raise PowerControllerError(
                    f"Failed to set poe-out={poe_out} on {interface_name}: {msg}"
                )

    async def _get_poe_out(self, interface_name: str) -> str:
        """Read current poe-out setting for an interface."""
        conn = await self._connect()
        response = await conn.call(
            "/interface/ethernet/poe/print",
            f"?name={interface_name}",
        )
        items = _parse_items(response)
        if items:
            return items[0].get("poe-out", "auto-on")
        return "auto-on"

    async def power_off(self, port: str) -> None:
        # Save current poe-out mode so power_on can restore it
        if port not in self._saved_poe_out:
            self._saved_poe_out[port] = await self._get_poe_out(port)
        logger.info("PoE OFF: %s on %s", port, self._host)
        await self._set_poe(port, "off")

    async def power_on(self, port: str) -> None:
        restore_mode = self._saved_poe_out.pop(port, "auto-on")
        logger.info("PoE ON: %s on %s (restoring %s)", port, self._host, restore_mode)
        await self._set_poe(port, restore_mode)

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
