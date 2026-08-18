"""Rockchip MaskROM / rockusb recovery over USB.

This is a separate subsystem from :mod:`defib.protocol`, and deliberately so.
Every protocol in that package is a UART boot-ROM dialect layered on the
byte-stream :class:`~defib.transport.base.Transport`.  Rockchip's boot ROM has
no UART download path at all — recovery is USB-only, and splits into two
stages that are not byte streams:

1. **MaskROM** — vendor control transfers (``bRequest=0x0C``) push a DDR init
   blob and then a "usbplug" blob into SRAM.  See :mod:`.maskrom`.
2. **rockusb** — the usbplug then speaks a USB Mass-Storage-shaped bulk
   protocol (CBW/CSW) with Rockchip opcodes.  See :mod:`.protocol`.

Because neither stage fits ``Transport``, none of this registers with the
``defib.protocols`` entry-point group.

The payoff for a test rig: when SPI NAND holds no valid IDB — an erased or
half-written flash — the boot ROM falls into MaskROM *by itself* at power-up,
with no button press and no strap.  Power-cycling the board is enough to make
it recoverable, which is what makes unattended recovery possible at all.

Protocol details were derived from the MIT-licensed xboot/xrock and the
BSD-2-licensed rkflashtool.  rkdeveloptool is GPL-2 and was **not** used as a
source for this implementation.
"""

from __future__ import annotations

from defib.rockusb.codec import RK_RC4_KEY, rc4, rk_crc16
from defib.rockusb.loader import LoaderBlobs, LoaderFormatError, parse_loader
from defib.rockusb.maskrom import CODE_471, CODE_472, build_maskrom_chunks
from defib.rockusb.protocol import (
    SECTOR_SIZE,
    CommandStatus,
    Opcode,
    ResetSubcode,
    RockusbError,
    build_cbw,
    parse_csw,
)

__all__ = [
    "CODE_471",
    "CODE_472",
    "RK_RC4_KEY",
    "SECTOR_SIZE",
    "CommandStatus",
    "LoaderBlobs",
    "LoaderFormatError",
    "Opcode",
    "ResetSubcode",
    "RockusbError",
    "build_cbw",
    "build_maskrom_chunks",
    "parse_csw",
    "parse_loader",
    "rc4",
    "rk_crc16",
]
