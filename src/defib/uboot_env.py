"""Helpers for U-Boot env handling during install.

The OpenIPC u-boot binaries ship with a compiled-in default env that
contains ``ethaddr=00:00:23:34:45:66``. When a camera boots with an
empty NAND env partition, u-boot loads that default into RAM. If anyone
then runs ``saveenv``, the bogus MAC is persisted to flash and from
then on every boot reads the same MAC. Multiple cameras converging on
``00:00:23:34:45:66`` is the visible symptom.

The mitigation here: detect the default (or missing) ``ethaddr`` and
replace it with a locally-administered random MAC before ``saveenv``.
"""

from __future__ import annotations

import re
import secrets

# CONFIG_ETHADDR baked into OpenIPC u-boot's default env. Found in the
# LZMA-compressed payload of u-boot-*-universal.bin (e.g. hi3516av200,
# hi3516cv300). Cameras whose env partition was empty when u-boot first
# saw them all converge on this MAC after the first saveenv.
OPENIPC_DEFAULT_ETHADDR = "00:00:23:34:45:66"

_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def is_unset_or_default_ethaddr(value: str | None) -> bool:
    """True if `value` is missing, blank, malformed, or the OpenIPC default."""
    if value is None:
        return True
    v = value.strip().lower()
    if not v:
        return True
    if not _MAC_RE.match(v):
        return True
    return v == OPENIPC_DEFAULT_ETHADDR.lower()


def generate_locally_administered_mac() -> str:
    """Generate a random unicast, locally-administered MAC.

    First octet has the locally-administered bit (bit 1) set and the
    multicast bit (bit 0) cleared, per IEEE 802. The remaining five
    octets are random. Always returns lowercase ``xx:xx:xx:xx:xx:xx``.
    """
    raw = bytearray(secrets.token_bytes(6))
    # Bit 0 (LSB of first octet): 0 = unicast.
    # Bit 1 (LSB+1):              1 = locally administered.
    raw[0] = (raw[0] & 0xFC) | 0x02
    return ":".join(f"{b:02x}" for b in raw)


def parse_printenv_value(response: str, var: str) -> str | None:
    """Pull the value of `var` out of a ``printenv VAR`` response.

    U-Boot prints lines like ``ethaddr=00:00:23:34:45:66`` (no quotes,
    one var per line). May be preceded/followed by prompt characters or
    download-mode framing. Returns the value or None if not found.
    """
    pattern = re.compile(rf"(?m)^\s*{re.escape(var)}=(.+?)\s*$")
    m = pattern.search(response)
    return m.group(1).strip() if m else None
