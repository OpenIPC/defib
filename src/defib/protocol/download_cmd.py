"""Client for U-Boot's download_process() protocol.

After serial boot, PRESTEP0 sets CONFIG_START_MAGIC ("DOWN") in
REG_START_FLAG.  U-Boot detects this and enters download_process() —
a simple XHEAD/XCMD framed command protocol that executes any U-Boot
command string (sf write, nand write, mmc write, tftpboot, etc.).

Protocol:
    Host sends XHEAD (5 bytes): [0xAB][len_hi][len_lo][crc16_hi][crc16_lo]
    Device responds: 0xAA (ACK) or 0x55 (NAK)
    Host sends XCMD (variable):  [0xCD][cmd_string...][crc16_hi][crc16_lo]
    Device responds: 0xAA (ACK) or 0x55 (NAK)
    Device executes command, then sends: "[EOT](OK)\\n" or "[EOT](ERROR)\\n"
"""

from __future__ import annotations

import logging

from defib.protocol.crc import calc_crc
from defib.transport.base import Transport, TransportTimeout

logger = logging.getLogger(__name__)

XHEAD = 0xAB
XCMD = 0xCD
ACK = 0xAA
NAK = 0x55


class DownloadCommandClient:
    """Client for U-Boot's download_process() XHEAD/XCMD protocol."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def wait_for_download_mode(self, timeout: float = 30.0) -> bool:
        """Wait for U-Boot to print 'start download process.' after boot."""
        import time

        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = await self._transport.read(256, timeout=0.5)
                buf.extend(data)
                if b"start download process" in buf:
                    logger.info("U-Boot entered download mode")
                    return True
            except TransportTimeout:
                continue
        logger.error("Timeout waiting for download mode (got %d bytes)", len(buf))
        if buf:
            logger.debug("Last output: %s", buf[-200:].decode("ascii", errors="replace"))
        return False

    async def send_command(
        self, cmd: str, timeout: float = 30.0
    ) -> tuple[bool, str]:
        """Send a U-Boot command and wait for [EOT] response.

        Returns (success, output_text).
        """
        cmd_bytes = cmd.encode("ascii")

        # XHEAD: [0xAB] [len_hi] [len_lo] [crc16_hi] [crc16_lo]
        # length = len(XCMD frame) - 3 = len(cmd_bytes) + 2 (CRC) + 1 (0xCD) - 3
        #        = len(cmd_bytes)
        xcmd_frame_len = 1 + len(cmd_bytes) + 2  # 0xCD + cmd + CRC16
        length = xcmd_frame_len - 3  # per protocol spec

        head = bytearray(5)
        head[0] = XHEAD
        head[1] = (length >> 8) & 0xFF
        head[2] = length & 0xFF
        crc = calc_crc(head[:3])
        head[3] = (crc >> 8) & 0xFF
        head[4] = crc & 0xFF

        logger.debug("XHEAD: cmd=%r len=%d", cmd, length)

        # Send XHEAD and wait for ACK
        for attempt in range(3):
            await self._transport.flush_input()
            await self._transport.write(bytes(head))
            try:
                ack = await self._transport.read(1, timeout=2.0)
                if ack[0] == ACK:
                    break
                logger.debug("XHEAD NAK (attempt %d/3)", attempt + 1)
            except TransportTimeout:
                logger.debug("XHEAD timeout (attempt %d/3)", attempt + 1)
        else:
            logger.error("XHEAD failed after 3 attempts")
            return False, ""

        # XCMD: [0xCD] [cmd_string...] [crc16_hi] [crc16_lo]
        xcmd = bytearray(1 + len(cmd_bytes))
        xcmd[0] = XCMD
        xcmd[1:] = cmd_bytes
        crc = calc_crc(xcmd)
        xcmd_full = bytes(xcmd) + bytes([(crc >> 8) & 0xFF, crc & 0xFF])

        await self._transport.write(xcmd_full)
        try:
            ack = await self._transport.read(1, timeout=2.0)
            if ack[0] != ACK:
                logger.error("XCMD NAK for %r", cmd)
                return False, ""
        except TransportTimeout:
            logger.error("XCMD timeout for %r", cmd)
            return False, ""

        logger.debug("XCMD ACKed, waiting for result...")

        # Read until [EOT](OK) or [EOT](ERROR)
        import time

        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = await self._transport.read(256, timeout=1.0)
                buf.extend(data)
                text = buf.decode("ascii", errors="replace")
                if "[EOT](OK)" in text:
                    output = text[:text.index("[EOT](OK)")]
                    logger.debug("CMD OK: %r -> %s", cmd, output[:100])
                    return True, output
                if "[EOT](ERROR)" in text:
                    output = text[:text.index("[EOT](ERROR)")]
                    logger.error("CMD ERROR: %r -> %s", cmd, output[:200])
                    return False, output
            except TransportTimeout:
                continue

        logger.error("Timeout waiting for [EOT] response to %r", cmd)
        return False, buf.decode("ascii", errors="replace")
