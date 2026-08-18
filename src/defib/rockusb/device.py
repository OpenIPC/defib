"""libusb plumbing for the two Rockchip USB stages.

Kept deliberately thin: everything that can be decided without a device on the
bus lives in :mod:`.maskrom`, :mod:`.protocol` and :mod:`.loader`.  What is
left here is enumeration, endpoint discovery and the blocking transfers, all
of which need real hardware.

``pyusb`` is an optional dependency — install the ``rockchip`` extra.  It is
imported lazily so that a defib install without it keeps working for every
UART-based SoC.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any

from defib.rockusb.protocol import (
    CSW_LENGTH,
    CommandStatus,
    Opcode,
    RockusbError,
    build_cbw,
    parse_csw,
)

logger = logging.getLogger(__name__)

ROCKCHIP_VID = 0x2207

# The rockusb interface advertises itself as vendor-specific. MaskROM exposes
# a barer descriptor, so interface matching is best-effort and we fall back to
# scanning every interface for a bulk pair.
ROCKUSB_CLASS = 0xFF
ROCKUSB_SUBCLASS = 0x06
ROCKUSB_PROTOCOL = 0x05


class DeviceMode(str, Enum):
    """Which of the two stages the device is currently answering."""

    MASKROM = "maskrom"
    LOADER = "loader"


class RockusbUsbError(RockusbError):
    """USB-level failure: no device, cannot claim, transfer error."""


def _require_usb() -> Any:
    try:
        import usb.core  # noqa: PLC0415
        import usb.util  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise RockusbUsbError(
            "pyusb is required for Rockchip USB recovery — "
            "install it with: pip install 'defib[rockchip]'"
        ) from e
    return usb


@dataclass
class FoundDevice:
    """A Rockchip device on the bus, and which stage it is in."""

    mode: DeviceMode
    bus: int
    address: int
    product_id: int
    handle: Any  # usb.core.Device
    port_numbers: tuple[int, ...] = ()

    @property
    def usb_path(self) -> str:
        """Physical topology path, e.g. ``1-4.2``.

        This is the only stable identity a Rockchip board has across the
        MaskROM-to-loader transition: the USB address is reassigned when the
        usbplug re-enumerates, and every board shares one VID:PID. The port
        path is where it is plugged in, so it survives.
        """
        if not self.port_numbers:
            return f"{self.bus}-?"
        return f"{self.bus}-{'.'.join(str(p) for p in self.port_numbers)}"

    def __str__(self) -> str:
        return (
            f"{self.mode.value} device {ROCKCHIP_VID:04x}:{self.product_id:04x} "
            f"at {self.usb_path} (bus {self.bus} addr {self.address})"
        )


def _classify(dev: Any) -> DeviceMode:
    """MaskROM or loader?

    Both stages enumerate under the same VID:PID — on RV1106, ``2207:110c`` —
    so the product id cannot be used. The boot ROM leaves the low bit of
    ``bcdUSB`` clear where the usbplug sets it, which is the same discriminator
    xrock relies on.
    """
    return DeviceMode.MASKROM if not (dev.bcdUSB & 0x0001) else DeviceMode.LOADER


def find_devices(usb_path: str | None = None) -> list[FoundDevice]:
    """Every Rockchip device on the bus, optionally filtered to one port path."""
    usb = _require_usb()

    out: list[FoundDevice] = []
    for dev in usb.core.find(idVendor=ROCKCHIP_VID, find_all=True):
        found = FoundDevice(
            mode=_classify(dev),
            bus=dev.bus,
            address=dev.address,
            product_id=dev.idProduct,
            handle=dev,
            port_numbers=tuple(getattr(dev, "port_numbers", None) or ()),
        )
        if usb_path is None or found.usb_path == usb_path:
            out.append(found)
    return out


def find_device(usb_path: str | None = None) -> FoundDevice | None:
    """A single Rockchip device, or None.

    Raises:
        RockusbUsbError: if more than one is present and no ``usb_path``
            narrows it down. Picking one arbitrarily would mean flashing
            whichever board happened to enumerate first, which on a rack is
            how you write firmware to the wrong device.
    """
    devices = find_devices(usb_path)
    if not devices:
        return None
    if len(devices) > 1:
        paths = ", ".join(sorted(d.usb_path for d in devices))
        raise RockusbUsbError(
            f"{len(devices)} Rockchip devices present ({paths}) — "
            "narrow it down with --usb-path"
        )
    return devices[0]


async def wait_for_device(
    timeout: float = 30.0,
    mode: DeviceMode | None = None,
    poll_interval: float = 0.25,
    usb_path: str | None = None,
) -> FoundDevice:
    """Poll until a matching device appears.

    Used both for the initial "power-cycle an erased board and catch it in
    MaskROM" step and for the re-enumeration that follows the usbplug upload.
    Pass ``usb_path`` for the latter: the board keeps its port path across
    re-enumeration, so pinning it stops the session hopping to a different
    board that happened to appear meanwhile.

    Args:
        timeout: seconds to keep looking.
        mode: require this stage; None accepts either.
        poll_interval: seconds between scans.
        usb_path: restrict to this physical port path (e.g. ``1-4.2``).

    Raises:
        RockusbUsbError: if nothing matching shows up in time, or if several
            candidates are present and ``usb_path`` does not disambiguate.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    seen: str | None = None

    while loop.time() < deadline:
        found = await asyncio.to_thread(find_device, usb_path)
        if found is not None:
            if mode is None or found.mode is mode:
                return found
            seen = str(found)
        await asyncio.sleep(poll_interval)

    want = f" in {mode.value} mode" if mode else ""
    where = f" at {usb_path}" if usb_path else ""
    if seen:
        raise RockusbUsbError(
            f"no Rockchip device{want}{where} after {timeout:.0f}s — "
            f"saw {seen} instead"
        )
    raise RockusbUsbError(
        f"no Rockchip device{want}{where} after {timeout:.0f}s. "
        "Power-cycle the board; an erased flash enters MaskROM on its own."
    )


class RockusbDevice:
    """An opened Rockchip device: control transfers and the bulk command loop.

    Call :meth:`open` before use and :meth:`close` after, or use it as a
    context manager.
    """

    def __init__(self, found: FoundDevice, timeout_ms: int = 5000) -> None:
        self._found = found
        self._dev = found.handle
        self._timeout_ms = timeout_ms
        self._interface: Any = None
        self._ep_in: Any = None
        self._ep_out: Any = None
        self._detached_interface: int | None = None

    @property
    def mode(self) -> DeviceMode:
        return self._found.mode

    def __enter__(self) -> RockusbDevice:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        """Claim the interface and locate the bulk endpoints.

        Endpoints are read from the descriptor rather than hardcoded; the
        conventional 0x02/0x81 pair is not guaranteed across usbplug builds.
        """
        usb = _require_usb()
        dev = self._dev

        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            # Already configured is fine; anything else is not.
            if e.errno not in (16, None):  # EBUSY
                raise RockusbUsbError(f"cannot configure {self._found}: {e}") from e

        cfg = dev.get_active_configuration()
        for intf in cfg:
            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: (
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT
                    and usb.util.endpoint_type(e.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK
                ),
            )
            ep_in = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: (
                    usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_IN
                    and usb.util.endpoint_type(e.bmAttributes)
                    == usb.util.ENDPOINT_TYPE_BULK
                ),
            )
            if ep_in is not None and ep_out is not None:
                self._interface, self._ep_in, self._ep_out = intf, ep_in, ep_out
                break

        if self._interface is None:
            # MaskROM legitimately has no bulk pair — control transfers only.
            if self._found.mode is DeviceMode.MASKROM:
                logger.debug("%s: no bulk endpoints, MaskROM control-only", self._found)
                return
            raise RockusbUsbError(
                f"{self._found}: no bulk IN/OUT endpoint pair found"
            )

        # Detach from the interface actually being claimed, not interface 0 —
        # the bulk pair is not guaranteed to live on the first one, and
        # detaching the wrong interface leaves the right one bound and the
        # claim below failing.
        number = self._interface.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(number):
                dev.detach_kernel_driver(number)
                self._detached_interface = number
        except (NotImplementedError, usb.core.USBError):
            # Not all backends/platforms implement this; only Linux binds a
            # kernel driver here in the first place.
            pass

        try:
            usb.util.claim_interface(dev, number)
        except usb.core.USBError as e:
            # Give the kernel driver back before bailing out. Leaving it
            # detached would strand the interface for whatever owned it,
            # turning a failed attempt into a lasting one.
            self._reattach_kernel_driver()
            raise RockusbUsbError(
                f"cannot claim interface {number} on {self._found}: {e} "
                "(need a udev rule for 2207:* or root)"
            ) from e

    def _reattach_kernel_driver(self) -> None:
        """Undo a detach, if we did one. Best-effort by nature."""
        if self._detached_interface is None:
            return
        try:
            self._dev.attach_kernel_driver(self._detached_interface)
        except Exception:  # pragma: no cover - platform dependent
            logger.debug(
                "could not reattach kernel driver to interface %d",
                self._detached_interface, exc_info=True,
            )
        finally:
            self._detached_interface = None

    def close(self) -> None:
        usb = _require_usb()
        try:
            if self._interface is not None:
                usb.util.release_interface(self._dev, self._interface.bInterfaceNumber)
            usb.util.dispose_resources(self._dev)
        except Exception:  # pragma: no cover - teardown is best-effort
            logger.debug("cleanup failed for %s", self._found, exc_info=True)
        self._reattach_kernel_driver()

    # -- MaskROM stage ----------------------------------------------------

    def control_write(self, code: int, payload: bytes) -> int:
        """One MaskROM code-upload control transfer.

        Returns the number of bytes the host actually pushed.

        Raises:
            RockusbUsbError: on a transfer error, or a short write. A
                truncated loader upload surfaces much later as a
                re-enumeration timeout that looks like a dead board, so it is
                caught where it happens.
        """
        try:
            written = int(self._dev.ctrl_transfer(
                bmRequestType=0x40,
                bRequest=0x0C,
                wValue=0x0000,
                wIndex=code,
                data_or_wLength=payload,
                timeout=self._timeout_ms,
            ))
            if written != len(payload):
                raise RockusbUsbError(
                    f"MaskROM control transfer short (code {code:#06x}): "
                    f"wrote {written} of {len(payload)} bytes"
                )
            return written
        except RockusbUsbError:
            raise
        except Exception as e:
            raise RockusbUsbError(
                f"MaskROM control transfer failed (code {code:#06x}, "
                f"{len(payload)} bytes): {e}"
            ) from e

    # -- rockusb bulk stage -----------------------------------------------

    def _write_bulk(self, data: bytes, what: str, opcode: Opcode | int) -> None:
        """Push ``data`` out the bulk endpoint, insisting all of it lands.

        pyusb returns how many bytes it managed; a short count means the
        device took less than we handed it, which for a payload write is a
        partial flash write wearing the costume of a successful one.
        """
        try:
            written = int(self._ep_out.write(data, self._timeout_ms))
        except Exception as e:
            raise RockusbUsbError(
                f"rockusb {what} write failed (opcode {int(opcode):#04x}): {e}"
            ) from e
        if written != len(data):
            raise RockusbUsbError(
                f"rockusb {what} write short (opcode {int(opcode):#04x}): "
                f"wrote {written} of {len(data)} bytes"
            )

    def _require_bulk(self) -> None:
        if self._ep_in is None or self._ep_out is None:
            raise RockusbUsbError(
                f"{self._found}: bulk commands need the usbplug running — "
                "device is still in MaskROM, send the loader first"
            )

    def command(
        self,
        opcode: Opcode | int,
        *,
        subcode: int = 0,
        address: int = 0,
        count: int = 0,
        data_out: bytes | None = None,
        read_length: int = 0,
        tolerate_disconnect: bool = False,
    ) -> bytes:
        """Run one CBW / optional data phase / CSW exchange.

        Returns any data read during an IN transfer, otherwise ``b""``.

        Raises:
            RockusbUsbError: on a transfer error, a non-zero status, or a
                short transfer. A device is entitled to move less than it was
                asked to and still report OK, reporting the shortfall in the
                status wrapper's residue; treating that as success is how a
                partial write gets announced as a finished install.
        """
        self._require_bulk()
        tag = secrets.randbits(32)
        direction_in = read_length > 0
        transfer_length = read_length if direction_in else len(data_out or b"")

        cbw = build_cbw(
            tag,
            opcode,
            subcode=subcode,
            address=address,
            count=count,
            transfer_length=transfer_length,
            direction_in=direction_in,
        )

        # The command itself must always get out intact. Only the status
        # wrapper may legitimately go missing, and only when the caller is
        # expecting the device to drop off the bus — see `tolerate_disconnect`.
        self._write_bulk(cbw, "command wrapper", opcode)
        try:
            payload = b""
            if direction_in:
                payload = bytes(self._ep_in.read(read_length, self._timeout_ms))
            elif data_out:
                self._write_bulk(data_out, "payload", opcode)
        except RockusbUsbError:
            raise
        except Exception as e:
            raise RockusbUsbError(
                f"rockusb data phase failed (opcode {int(opcode):#04x}): {e}"
            ) from e

        try:
            csw = bytes(self._ep_in.read(CSW_LENGTH, self._timeout_ms))
        except Exception as e:
            if tolerate_disconnect:
                logger.debug(
                    "no status wrapper for opcode %#04x — device already gone: %s",
                    int(opcode), e,
                )
                return b""
            raise RockusbUsbError(
                f"rockusb status read failed (opcode {int(opcode):#04x}): {e}"
            ) from e

        _, residue, status = parse_csw(csw, expected_tag=tag)
        if status != CommandStatus.OK:
            raise RockusbUsbError(
                f"rockusb command {int(opcode):#04x} failed "
                f"(status {status}, residue {residue})"
            )
        if residue:
            raise RockusbUsbError(
                f"rockusb command {int(opcode):#04x} moved "
                f"{transfer_length - residue} of {transfer_length} bytes "
                f"(residue {residue})"
            )
        if direction_in and len(payload) != read_length:
            raise RockusbUsbError(
                f"rockusb command {int(opcode):#04x} returned "
                f"{len(payload)} of {read_length} bytes"
            )
        return payload
