"""libusb plumbing for the GM8126 updater. Needs pyusb (``defib[grainmedia]``)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from defib.grainmedia.protocol import (
    CSW_LENGTH,
    GRAINMEDIA_VID,
    Command,
    GrainMediaError,
    build_cbw,
    parse_csw,
)

logger = logging.getLogger(__name__)


class GrainMediaUsbError(GrainMediaError):
    """USB-level failure: no device, cannot claim, transfer error."""


def _require_usb() -> Any:
    try:
        import usb.core
        import usb.util
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise GrainMediaUsbError(
            "pyusb is required for GM8126 USB recovery — "
            "install it with: pip install 'defib[grainmedia]'"
        ) from e
    return usb


@dataclass
class FoundDevice:
    bus: int
    address: int
    product_id: int
    handle: Any  # usb.core.Device
    port_numbers: tuple[int, ...] = ()

    @property
    def usb_path(self) -> str:
        """Physical topology path, e.g. ``1-4.2``."""
        return f"{self.bus}-{'.'.join(str(p) for p in self.port_numbers) or '?'}"

    def __str__(self) -> str:
        return (
            f"GM8126 updater {GRAINMEDIA_VID:04x}:{self.product_id:04x} "
            f"at {self.usb_path} (bus {self.bus} addr {self.address})"
        )


def find_device(
    recovery_ids: Sequence[int], usb_path: str | None = None
) -> FoundDevice | None:
    """The one updater on the bus, or None. Several without a path is an error."""
    usb = _require_usb()
    try:
        present = list(usb.core.find(idVendor=GRAINMEDIA_VID, find_all=True))
    except Exception as e:
        # NoBackendError (no libusb) is not a USBError.
        raise GrainMediaUsbError(f"cannot enumerate USB devices: {e}") from e
    found = [
        FoundDevice(
            bus=dev.bus,
            address=dev.address,
            product_id=dev.idProduct,
            handle=dev,
            port_numbers=tuple(getattr(dev, "port_numbers", None) or ()),
        )
        for dev in present
        if dev.idProduct in recovery_ids
    ]
    if usb_path:
        found = [f for f in found if f.usb_path == usb_path]
    if len(found) > 1:
        paths = ", ".join(sorted(f.usb_path for f in found))
        raise GrainMediaUsbError(
            f"{len(found)} GM8126 updaters present ({paths}) — narrow it down with --usb-path"
        )
    return found[0] if found else None


async def wait_for_device(
    recovery_ids: Sequence[int],
    timeout: float = 30.0,
    usb_path: str | None = None,
    poll_interval: float = 0.25,
) -> FoundDevice:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        found = await asyncio.to_thread(find_device, recovery_ids, usb_path)
        if found is not None:
            return found
        if loop.time() >= deadline:
            where = f" at {usb_path}" if usb_path else ""
            raise GrainMediaUsbError(
                f"no GM8126 updater{where} after {timeout:.0f}s. The camera enters it at "
                "power-on when flash has no valid header, or with the boot straps at USB; "
                "its USB port must be wired as a device"
            )
        await asyncio.sleep(poll_interval)


class GrainMediaDevice:
    """An opened updater: the bulk-only command loop."""

    def __init__(self, found: FoundDevice, timeout_ms: int = 10000) -> None:
        self._found = found
        self._dev = found.handle
        self._timeout_ms = timeout_ms
        self._number: int | None = None
        self._ep_in: Any = None
        self._ep_out: Any = None
        self._detached = False
        self._desynchronised = False

    @property
    def info(self) -> FoundDevice:
        return self._found

    def open(self) -> None:
        usb = _require_usb()
        dev = self._dev
        try:
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            for intf in cfg:
                ends: dict[int, Any] = {}
                for ep in intf:
                    if (
                        usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK
                    ):
                        ends.setdefault(usb.util.endpoint_direction(ep.bEndpointAddress), ep)
                if len(ends) == 2:
                    self._number = intf.bInterfaceNumber
                    self._ep_in = ends[usb.util.ENDPOINT_IN]
                    self._ep_out = ends[usb.util.ENDPOINT_OUT]
                    break
            if self._number is None:
                raise GrainMediaUsbError(f"{self._found}: no bulk IN/OUT endpoint pair")
            # It is a mass-storage interface, so usb-storage binds it.
            try:
                if dev.is_kernel_driver_active(self._number):
                    dev.detach_kernel_driver(self._number)
                    self._detached = True
            except NotImplementedError:
                pass
            usb.util.claim_interface(dev, self._number)
        except usb.core.USBError as e:
            self.close()
            raise GrainMediaUsbError(
                f"cannot open {self._found}: {e} (Linux needs root or a udev rule for "
                f"{GRAINMEDIA_VID:04x}:*; Windows needs the WinUSB driver)"
            ) from e
        except GrainMediaUsbError:
            self.close()
            raise

    def close(self) -> None:
        usb = _require_usb()
        try:
            if self._number is not None:
                usb.util.release_interface(self._dev, self._number)
            usb.util.dispose_resources(self._dev)
        except Exception:  # pragma: no cover - teardown is best-effort
            logger.debug("cleanup failed for %s", self._found, exc_info=True)
        if self._detached:
            try:
                self._dev.attach_kernel_driver(self._number)
            except Exception:  # pragma: no cover - platform dependent
                logger.debug("could not reattach the kernel driver", exc_info=True)
            self._detached = False

    def _read(self, length: int) -> bytes:
        # A buffer that is not a whole number of packets overflows when the
        # device answers with a full one.
        mps = int(getattr(self._ep_in, "wMaxPacketSize", 512) or 512)
        return bytes(self._ep_in.read((length + mps - 1) // mps * mps, self._timeout_ms))

    @staticmethod
    def _is_stall(error: Exception) -> bool:
        return getattr(error, "errno", None) == 32

    def _read_status(self) -> bytes:
        try:
            return bytes(self._read(CSW_LENGTH))
        except Exception as e:
            if not self._is_stall(e):
                raise
            # Bulk-only transport: clear the halt and ask once more.
            self._ep_in.clear_halt()
            return bytes(self._read(CSW_LENGTH))

    def command(self, command: Command, data_out: bytes = b"") -> bytes:
        """Run one command and return the data it sent.

        Raises:
            GrainMediaUsbError: on a transfer error, a short transfer or a
                failed status.
        """
        name = command.cdb[:2].hex(" ")
        if self._desynchronised:
            raise GrainMediaUsbError(
                f"command {name} not sent: an earlier transfer was lost, "
                "power-cycle the camera and start again"
            )
        if not command.direction_in and len(data_out) != command.length:
            raise ValueError(f"command {name} takes {command.length} bytes, got {len(data_out)}")
        tag = secrets.randbits(32)
        data = b""
        stalled = False
        # A command whose status never arrived may still run, and its status
        # would then answer the next one: stay unusable until the end is read.
        self._desynchronised = True
        try:
            cbw = build_cbw(tag, command)
            if int(self._ep_out.write(cbw, self._timeout_ms)) != len(cbw):
                raise GrainMediaUsbError(f"command {name}: short command wrapper")
            try:
                if command.direction_in and command.length:
                    data = self._read(command.length)
                elif data_out:
                    sent = int(self._ep_out.write(data_out, self._timeout_ms))
                    if sent != len(data_out):
                        raise GrainMediaUsbError(
                            f"command {name}: sent {sent} of {len(data_out)} bytes"
                        )
            except GrainMediaUsbError:
                raise
            except Exception as e:
                if not self._is_stall(e):
                    raise
                stalled = True
                (self._ep_in if command.direction_in else self._ep_out).clear_halt()
            residue, status = parse_csw(self._read_status(), tag)
        except GrainMediaUsbError:
            raise
        except GrainMediaError as e:
            raise GrainMediaUsbError(f"command {name}: {e}") from e
        except Exception as e:
            raise GrainMediaUsbError(f"command {name} failed: {e}") from e
        self._desynchronised = False
        if status or stalled:
            raise GrainMediaUsbError(f"command {name} was refused (status {status})")
        if residue or (command.direction_in and len(data) != command.length):
            moved = len(data) if command.direction_in else command.length - residue
            raise GrainMediaUsbError(f"command {name} moved {moved} of {command.length} bytes")
        return data
