"""Platform-detecting serial transport factory.

Handles platform-specific workarounds:
- macOS: ACK byte correction (0x55 → 0xAA), stale byte drain, DTR/RTS toggle
- Windows: COM port naming conventions
- Linux: standard behavior (no workarounds needed)
"""

from __future__ import annotations

import logging
import sys

import serial

from defib.transport.base import Transport, TransportError
from defib.transport.serial import SerialTransport

logger = logging.getLogger(__name__)


class MacOSSerialTransport(SerialTransport):
    """Serial transport with macOS-specific workarounds.

    Known issue (GitHub OpenIPC/burn#16): macOS USB-serial drivers
    sometimes return 0x55 instead of the expected 0xAA ACK byte.
    This transport applies configurable corrections.
    """

    def __init__(
        self,
        port: serial.Serial,
        ack_correction: bool = True,
        extra_delay_ms: float = 5.0,
    ) -> None:
        super().__init__(port)
        self._ack_correction = ack_correction
        self._extra_delay_ms = extra_delay_ms
        self._in_handshake = True  # Correction only during handshake phase

    async def read(self, size: int, timeout: float | None = None) -> bytes:
        data = await super().read(size, timeout)
        if self._ack_correction and self._in_handshake:
            corrected = data.replace(b"\x55", b"\xaa")
            if corrected != data:
                logger.info(
                    "macOS ACK correction: replaced 0x55 → 0xAA in %d bytes",
                    len(data) - len(data.replace(b"\x55", b"")),
                )
            return corrected
        return data

    def set_handshake_phase(self, active: bool) -> None:
        """Enable/disable ACK correction (should be disabled after handshake)."""
        self._in_handshake = active

    @classmethod
    async def create(
        cls,
        device: str,
        baudrate: int = 115200,
        ack_correction: bool = True,
    ) -> MacOSSerialTransport:
        """Open a serial port with macOS workarounds."""
        try:
            port = serial.Serial(
                port=device,
                baudrate=baudrate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=None,
            )
        except serial.SerialException as e:
            raise TransportError(f"Failed to open {device}: {e}") from e

        transport = cls(port, ack_correction=ack_correction)

        # Drain stale bytes
        logger.info("macOS: draining stale bytes from %s", device)
        port.timeout = 0.1
        try:
            stale = port.read(4096)
            if stale:
                logger.debug("Drained %d stale bytes", len(stale))
        except Exception:
            pass

        # Toggle DTR/RTS to reset adapter state
        try:
            port.dtr = False
            port.rts = False
            import asyncio
            await asyncio.sleep(0.05)
            port.dtr = True
            port.rts = True
            await asyncio.sleep(0.05)
            logger.debug("macOS: toggled DTR/RTS")
        except Exception:
            logger.debug("macOS: DTR/RTS toggle not supported on this adapter")

        # Drain again after toggle
        try:
            stale = port.read(4096)
        except Exception:
            pass

        port.timeout = None
        return transport


async def create_transport(
    device: str,
    baudrate: int = 115200,
    force_platform: str | None = None,
) -> Transport:
    """Create a platform-appropriate transport.

    Args:
        device: Serial port device path (e.g., /dev/ttyUSB0, COM3),
                Unix socket path with ``socket://`` prefix (e.g.,
                ``socket:///tmp/qemu.sock``), raw TCP endpoint with
                ``tcp://`` prefix, or RFC 2217 endpoint with
                ``rfc2217://`` prefix (recommended for OpenIPC Vectis
                ≥1.2.0 — binary safe + out-of-band RTS/DTR control).
        baudrate: Baud rate (default 115200, ignored for non-RFC-2217
                sockets — for ``rfc2217://`` it is sent during open).
        force_platform: Override platform detection ("linux", "darwin", "win32").

    Returns:
        An appropriate Transport implementation.
    """
    # Unix socket transport (for QEMU chardev sockets)
    if device.startswith("socket://"):
        from defib.transport.socket import SocketTransport
        path = device[len("socket://"):]
        logger.info("Using SocketTransport: %s", path)
        return await SocketTransport.create(path)

    # TCP socket transport (raw, no escaping — for non-RFC-2217 bridges
    # or for compatibility with old Vectis builds without RFC 2217).
    if device.startswith("tcp://"):
        from defib.transport.socket import SocketTransport
        endpoint = device[len("tcp://"):]
        host, _, port_str = endpoint.rpartition(":")
        if not host or not port_str:
            raise TransportError(
                f"tcp:// transport needs host:port (got '{device}')"
            )
        try:
            port_num = int(port_str)
        except ValueError as e:
            raise TransportError(
                f"tcp:// port is not a number: '{port_str}'"
            ) from e
        logger.info("Using TCP SocketTransport: %s:%d", host, port_num)
        return await SocketTransport.create_tcp(host, port_num)

    # RFC 2217 transport (binary-safe + modem-control sub-options).
    # Pass the URL through pyserial's rfc2217 backend; baud rate is
    # negotiated via SET-BAUDRATE during open.  Used by VectisController.
    if device.startswith("rfc2217://"):
        from defib.transport.rfc2217 import Rfc2217Transport
        logger.info("Using RFC 2217 transport: %s", device)
        return await Rfc2217Transport.create(device, baudrate=baudrate)

    # Rack pod: TCP UART bridge + HTTP control plane for baud sync.
    # URL form: rack://host[:bridge_port][?api=http_port].  Defaults
    # are 9000 / 8080.  Differs from tcp:// only in that set_baudrate()
    # POSTs to /uart/baud, so the on-device agent's set_baud rendezvous
    # actually syncs both ends.
    if device.startswith("rack://"):
        from defib.transport.rack import RackTransport
        endpoint = device[len("rack://"):]
        # Optional ?api=NNN suffix
        api_port = 8080
        if "?" in endpoint:
            endpoint, _, query = endpoint.partition("?")
            for kv in query.split("&"):
                if kv.startswith("api="):
                    try:
                        api_port = int(kv[len("api="):])
                    except ValueError as e:
                        raise TransportError(
                            f"rack:// api port is not a number: {kv!r}"
                        ) from e
        if ":" in endpoint:
            host, _, bp = endpoint.partition(":")
            try:
                bridge_port = int(bp)
            except ValueError as e:
                raise TransportError(
                    f"rack:// bridge port is not a number: {bp!r}"
                ) from e
        else:
            host = endpoint
            bridge_port = 9000
        if not host:
            raise TransportError(f"rack:// transport needs a host (got '{device}')")
        logger.info(
            "Using RackTransport: %s:%d (api :%d)", host, bridge_port, api_port,
        )
        return await RackTransport.create_rack(host, bridge_port, api_port)

    platform = force_platform or sys.platform

    if platform == "darwin":
        logger.info("macOS detected: using MacOSSerialTransport with ACK correction")
        return await MacOSSerialTransport.create(device, baudrate)
    else:
        logger.info("Using standard SerialTransport for %s", platform)
        return await SerialTransport.create(device, baudrate)


def normalize_port_name(device: str) -> str:
    """Normalize serial port names across platforms.

    - Windows: Accepts COM3 or \\\\.\\COM3 format
    - Linux/macOS: Passes through as-is
    """
    if sys.platform == "win32" and device.upper().startswith("COM"):
        port_num = device[3:]
        if port_num.isdigit() and int(port_num) >= 10:
            # Windows needs \\.\COMxx for ports >= 10
            return f"\\\\.\\{device.upper()}"
    return device
