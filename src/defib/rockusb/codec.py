"""Wire codecs for the Rockchip MaskROM stage: CRC-16 and RC4.

Both are needed only by :mod:`defib.rockusb.maskrom` — the rockusb bulk stage
carries no checksum of its own (USB already provides one) and is never
encrypted.
"""

from __future__ import annotations

from defib.protocol.crc import CRC_TABLE

# Rockchip's boot ROM fixes this key for the MaskROM code path.  Published in
# rkflashtool's README.maskrom as the argument to ``openssl rc4 -K``, and
# identical to the key constant in xboot/xrock.
RK_RC4_KEY = bytes(
    [124, 78, 3, 4, 85, 5, 9, 7, 45, 44, 123, 56, 23, 13, 23, 17]
)

# CRC-16/CCITT seed used by the MaskROM loader.  Note this is *not* the same
# variant as :func:`defib.protocol.crc.calc_crc`, which HiSilicon seeds at 0
# and finalises with two zero bytes.  Only the polynomial table is shared.
CRC16_INIT = 0xFFFF


def rk_crc16(data: bytes | bytearray, crc: int = CRC16_INIT) -> int:
    """CRC-16/CCITT as the Rockchip boot ROM computes it.

    Poly 0x1021, seeded 0xFFFF, MSB-first, no final XOR — matching
    ``rkcrc16()`` in rkflashtool's BSD-2 ``rkcrc.h``::

        crc = (crc << 8) ^ crc16table[(crc >> 8) ^ *buf++];
    """
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc & 0xFFFF


def rc4(data: bytes | bytearray, key: bytes = RK_RC4_KEY) -> bytes:
    """Plain RC4. Self-inverse, so this both encrypts and decrypts.

    Only used when a loader's header does *not* set the "RC4 disabled" flag.
    Newer parts — RV1106 among them — ship loaders built with RC4 off, so this
    path is normally dead for that SoC.
    """
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]

    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)
