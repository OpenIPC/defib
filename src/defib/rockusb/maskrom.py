"""MaskROM stage: framing for the two SRAM code uploads.

The boot ROM accepts code via vendor control transfers rather than any bulk
endpoint::

    bmRequestType = 0x40   (vendor, host->device, device recipient)
    bRequest      = 0x0C
    wValue        = 0x0000
    wIndex        = 0x0471 | 0x0472

``0x0471`` carries the DDR init blob, which runs in SRAM, brings up DRAM and
returns to the boot ROM.  ``0x0472`` then carries the "usbplug" blob, which
takes over the USB device and starts answering the bulk protocol in
:mod:`defib.rockusb.protocol`.

This module deliberately holds no USB code — :func:`build_maskrom_chunks`
turns a blob into the exact sequence of control-transfer payloads, so the
framing (which is where the fiddly parts live) is unit-testable with no
hardware and no libusb.
"""

from __future__ import annotations

import struct

from defib.rockusb.codec import rc4, rk_crc16

CODE_471 = 0x0471
CODE_472 = 0x0472

# Boot ROM accepts at most this much per control transfer.
CHUNK_SIZE = 4096


def build_maskrom_chunks(
    blob: bytes,
    *,
    use_rc4: bool = False,
) -> list[bytes]:
    """Frame ``blob`` into the control-transfer payloads the boot ROM expects.

    The blob is optionally RC4'd, gets a big-endian CRC-16 appended, and is
    then split into 4096-byte chunks.  Two size-dependent quirks, both copied
    from the reference implementations, are folded in here:

    * ``len(blob) % 4096 == 4095`` — a zero byte is appended *before* the CRC
      is computed.  Without it the two CRC bytes would straddle a chunk
      boundary, which the boot ROM does not accept.
    * ``len(blob) % 4096 == 4094`` — appending the CRC makes the payload an
      exact multiple of the chunk size, leaving no short packet to signal the
      end of the transfer.  A trailing one-byte chunk is emitted to terminate
      it.

    Args:
        blob: raw DDR-init or usbplug image.
        use_rc4: encrypt before checksumming.  Loaders whose header sets the
            "RC4 disabled" flag — which includes every RV1106 loader Rockchip
            ships — must leave this off.

    Returns:
        Payloads to send, in order, each as one control transfer.
    """
    payload = bytearray(blob)

    remainder = len(payload) % CHUNK_SIZE
    needs_terminator = remainder == CHUNK_SIZE - 2
    if remainder == CHUNK_SIZE - 1:
        payload.append(0x00)

    if use_rc4:
        # Whole-buffer, matching xrock. rkdeveloptool is widely read as
        # encrypting per 4096-byte block instead; the two agree only when the
        # blob is under one chunk. Untested here because RV1106 ships RC4-off
        # loaders — verify against hardware before trusting this on a part
        # that actually needs encryption.
        payload = bytearray(rc4(bytes(payload)))

    payload += struct.pack(">H", rk_crc16(payload))

    chunks = [
        bytes(payload[i : i + CHUNK_SIZE])
        for i in range(0, len(payload), CHUNK_SIZE)
    ]
    if needs_terminator:
        chunks.append(b"\x00")
    return chunks
