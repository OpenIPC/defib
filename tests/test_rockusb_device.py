"""Tests for Rockchip USB device selection and transport behaviour.

Most of what is pinned here was learnt from a Luckfox Pico Max on the bench:
which device on the bus is actually a recovery target, which stage it is in,
and the several ways an inherited or half-finished transaction makes a
healthy board look dead.
"""

import pytest

from defib.rockusb.device import DeviceMode, FoundDevice
from defib.rockusb.protocol import CSW_SIGNATURE, Opcode


class TestFoundDevice:
    def _dev(self, bus=1, ports=(4, 2), mode=DeviceMode.MASKROM):
        return FoundDevice(
            mode=mode, bus=bus, address=7, product_id=0x110C,
            handle=None, port_numbers=ports,
        )

    def test_usb_path_is_the_topology_path(self):
        assert self._dev().usb_path == "1-4.2"

    def test_usb_path_survives_readdressing(self):
        """Address changes when the usbplug re-enumerates; the port path is
        what identifies the same physical board across that."""
        before = self._dev()
        after = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=9, product_id=0x110C,
            handle=None, port_numbers=(4, 2),
        )
        assert before.usb_path == after.usb_path
        assert before.address != after.address

    def test_distinct_ports_are_distinct_paths(self):
        assert self._dev(ports=(4, 2)).usb_path != self._dev(ports=(4, 3)).usb_path

    def test_missing_port_numbers_degrade_visibly(self):
        assert self._dev(ports=()).usb_path == "1-?"

    def test_str_mentions_the_path(self):
        assert "1-4.2" in str(self._dev())


class TestRecoveryIdFilter:
    """A running Luckfox presents 2207:0019 — an RNDIS+ADB gadget sharing the
    vendor id, whose bcdUSB of 0x0200 makes it look like MaskROM to the
    bcdUSB test. Found on real hardware: without a product-id filter, defib
    would have uploaded a loader into a healthy booted board.
    """

    class _FakeUsbDevice:
        def __init__(self, pid, bcd=0x0200, strings=False):
            self.idProduct = pid
            self.bcdUSB = bcd
            # MaskROM ships no string descriptors; anything past it names
            # itself. Measured on an RV1106: bcdUSB is 0x0200 for both, so
            # these indices are the only thing separating the two stages.
            self.iManufacturer = 1 if strings else 0
            self.iProduct = 2 if strings else 0
            self.bus = 7
            self.address = 3
            self.port_numbers = (1,)

    def _patch(self, monkeypatch, devices):
        import defib.rockusb.device as mod

        class FakeCore:
            @staticmethod
            def find(**kwargs):
                return list(devices)

        monkeypatch.setattr(mod, "_require_usb", lambda: type("U", (), {"core": FakeCore}))

    def test_runtime_adb_gadget_ignored(self, monkeypatch):
        from defib.rockusb.device import find_devices

        self._patch(monkeypatch, [self._FakeUsbDevice(0x0019)])
        assert find_devices(recovery_ids=[0x110C]) == []

    def test_recovery_device_still_found(self, monkeypatch):
        from defib.rockusb.device import find_devices

        self._patch(monkeypatch, [self._FakeUsbDevice(0x110C)])
        found = find_devices(recovery_ids=[0x110C])
        assert len(found) == 1
        assert found[0].product_id == 0x110C

    def test_recovery_device_picked_out_of_a_mixed_bus(self, monkeypatch):
        from defib.rockusb.device import find_devices

        self._patch(
            monkeypatch,
            [self._FakeUsbDevice(0x0019), self._FakeUsbDevice(0x110C)],
        )
        found = find_devices(recovery_ids=[0x110C])
        assert [d.product_id for d in found] == [0x110C]

    def test_no_ids_means_no_filter(self, monkeypatch):
        """Back-compat: callers that pass nothing keep the old broad search."""
        from defib.rockusb.device import find_devices

        self._patch(monkeypatch, [self._FakeUsbDevice(0x0019)])
        assert len(find_devices()) == 1

    def test_rv1106_profile_declares_the_recovery_id(self):
        from defib.profiles.loader import load_profile

        assert load_profile("rv1106").usb_recovery_ids == [0x110C]


class TestModeClassification:
    """Measured on an RV1106: both stages report bcdUSB 0x0200, so xrock's
    low-bit test calls a running usbplug MaskROM. Only the string descriptors
    tell them apart.
    """

    def _dev(self, *, strings):
        return TestRecoveryIdFilter._FakeUsbDevice(0x110C, strings=strings)

    def test_bare_descriptor_is_maskrom(self):
        from defib.rockusb.device import DeviceMode, _classify

        assert _classify(self._dev(strings=False)) is DeviceMode.MASKROM

    def test_named_device_is_loader(self):
        from defib.rockusb.device import DeviceMode, _classify

        assert _classify(self._dev(strings=True)) is DeviceMode.LOADER

    def test_bcdusb_is_not_consulted(self):
        """Both real stages report 0x0200; keying on it regresses the bug."""
        from defib.rockusb.device import DeviceMode, _classify

        maskrom = TestRecoveryIdFilter._FakeUsbDevice(0x110C, bcd=0x0200, strings=False)
        loader = TestRecoveryIdFilter._FakeUsbDevice(0x110C, bcd=0x0200, strings=True)
        assert _classify(maskrom) is DeviceMode.MASKROM
        assert _classify(loader) is DeviceMode.LOADER

    def test_manufacturer_alone_is_enough(self):
        from defib.rockusb.device import DeviceMode, _classify

        dev = TestRecoveryIdFilter._FakeUsbDevice(0x110C, strings=False)
        dev.iManufacturer = 1
        assert _classify(dev) is DeviceMode.LOADER


class TestResidueScope:
    """Measured on an RV1106 usbplug: residue is only honoured on the LBA
    path. Everything else reports its transfer length written big-endian —
    i.e. "none of it arrived" — while the data plainly did arrive.
    """

    def test_lba_opcodes_are_checked(self):
        from defib.rockusb.protocol import Opcode, residue_is_meaningful

        for op in (Opcode.READ_LBA, Opcode.WRITE_LBA, Opcode.ERASE_LBA):
            assert residue_is_meaningful(op)

    def test_other_opcodes_are_not(self):
        from defib.rockusb.protocol import Opcode, residue_is_meaningful

        for op in (
            Opcode.TEST_UNIT_READY, Opcode.READ_FLASH_ID,
            Opcode.READ_CAPABILITY, Opcode.READ_CHIP_INFO,
            Opcode.READ_FLASH_INFO, Opcode.RESET_DEVICE,
        ):
            assert not residue_is_meaningful(op)

    def test_observed_garbage_residues_would_be_ignored(self):
        """The literal values seen on the bench, each the transfer length
        byte-swapped. Enforcing residue here would break every probe."""
        from defib.rockusb.protocol import Opcode, residue_is_meaningful

        observed = {
            Opcode.TEST_UNIT_READY: 0x06000000,
            Opcode.READ_FLASH_ID: 0x05000000,
            Opcode.READ_CAPABILITY: 0x08000000,
            Opcode.READ_FLASH_INFO: 0x0B000000,
        }
        for op, residue in observed.items():
            assert residue != 0 and not residue_is_meaningful(op)


class TestStaleInputDrain:
    """A recovery tool is routinely pointed at a device an earlier attempt
    abandoned mid-transaction. The unread status wrapper it left behind gets
    read as the next command's data phase — 13 bytes into a 5-byte buffer is
    [Errno 75] Overflow, and everything after it desynchronises.
    """

    class _Ep:
        wMaxPacketSize = 512

        def __init__(self, queued):
            self.queued = list(queued)
            self.reads = 0

        def read(self, size, timeout=None):
            self.reads += 1
            if not self.queued:
                raise TimeoutError("nothing waiting")
            return self.queued.pop(0)

    def _device(self, queued):
        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        d = RockusbDevice(found)
        d._ep_in = self._Ep(queued)
        return d

    def test_stale_bytes_are_discarded(self):
        d = self._device([b"USBS" + b"\x00" * 9])
        d._drain_stale_input()
        assert d._ep_in.queued == []

    def test_drain_stops_at_the_first_timeout(self):
        d = self._device([])
        d._drain_stale_input()
        assert d._ep_in.reads == 1

    def test_drain_is_bounded(self):
        """A device stuck streaming must not hang the open."""
        d = self._device([b"x" * 512] * 100)
        d._drain_stale_input()
        assert d._ep_in.reads <= 8

    def test_no_endpoint_is_harmless(self):
        """MaskROM is reached before endpoints are known."""
        d = self._device([])
        d._ep_in = None
        d._drain_stale_input()


class TestBulkReadRounding:
    """Bulk IN buffers must be a multiple of the endpoint's max packet size."""

    class _Ep:
        wMaxPacketSize = 512

        def __init__(self):
            self.requested = None

        def read(self, size, timeout=None):
            self.requested = size
            return b"\x5a" * size

    def _device(self):
        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        d = RockusbDevice(found)
        d._ep_in = self._Ep()
        return d

    def test_short_read_is_rounded_up(self):
        d = self._device()
        out = d._read_bulk(5)
        assert d._ep_in.requested == 512
        assert len(out) == 5

    def test_exact_multiple_is_unchanged(self):
        d = self._device()
        d._read_bulk(1024)
        assert d._ep_in.requested == 1024

    def test_partial_sector_rounds_to_next_packet(self):
        d = self._device()
        d._read_bulk(513)
        assert d._ep_in.requested == 1024


class TestResidueHandling:
    """A device may move less than asked and still report status OK."""

    class _FakeEndpoint:
        def __init__(self, replies=None):
            self.written = []
            self._replies = list(replies or [])

        def write(self, data, timeout=None):
            self.written.append(bytes(data))
            return len(data)

        def read(self, size, timeout=None):
            return self._replies.pop(0)

    def _device(self, residue: int, status: int = 0):
        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        device = RockusbDevice(found)
        device._ep_out = self._FakeEndpoint()
        # CSW tag is echoed from the CBW the device just received.
        device._ep_in = self._FakeEndpoint()

        import struct

        def read(size, timeout=None):
            cbw = device._ep_out.written[0]
            tag = struct.unpack_from("<I", cbw, 4)[0]
            return CSW_SIGNATURE + struct.pack("<IIB", tag, residue, status)

        device._ep_in.read = read  # type: ignore[method-assign]
        return device

    def test_full_transfer_accepted(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(residue=0)
        try:
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)
        except RockusbUsbError as e:  # pragma: no cover - guards a regression
            pytest.fail(f"clean transfer rejected: {e}")

    def test_short_transfer_rejected(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(residue=256)
        with pytest.raises(RockusbUsbError, match="residue 256"):
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)

    def test_short_transfer_reports_what_moved(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(residue=256)
        with pytest.raises(RockusbUsbError, match="256 of 512 bytes"):
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)

    def test_failure_status_still_wins(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(residue=0, status=1)
        with pytest.raises(RockusbUsbError, match="failed"):
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)


class TestShortBulkWrites:
    """pyusb reports how many bytes it managed; a short count on a payload is
    a partial flash write dressed as a successful one."""

    def _device(self, write_returns):
        import struct

        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        device = RockusbDevice(found)
        written: list[bytes] = []

        class Out:
            def write(self, data, timeout=None):
                written.append(bytes(data))
                return write_returns(bytes(data))

        class In:
            def read(self, size, timeout=None):
                tag = struct.unpack_from("<I", written[0], 4)[0]
                return CSW_SIGNATURE + struct.pack("<IIB", tag, 0, 0)

        device._ep_out = Out()
        device._ep_in = In()
        return device

    def test_full_write_accepted(self):
        device = self._device(lambda d: len(d))
        device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)

    def test_short_payload_write_rejected(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(lambda d: len(d) if len(d) == 31 else len(d) - 8)
        with pytest.raises(RockusbUsbError, match="payload write short"):
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)

    def test_short_command_write_rejected(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(lambda d: len(d) - 1)
        with pytest.raises(RockusbUsbError, match="command wrapper write short"):
            device.command(Opcode.WRITE_LBA, address=0, count=1, data_out=b"\x00" * 512)


class TestResetFailureHandling:
    """A board may vanish acknowledging its own reset — but only that is
    tolerable. A reset that never went out must not be reported as done."""

    def _device(self, *, send_fails=False, csw_fails=False, status=0):
        import struct

        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        device = RockusbDevice(found)
        written: list[bytes] = []

        class Out:
            def write(self, data, timeout=None):
                if send_fails:
                    raise OSError("pipe error")
                written.append(bytes(data))
                return len(data)

        class In:
            def read(self, size, timeout=None):
                if csw_fails:
                    raise OSError("no such device")
                tag = struct.unpack_from("<I", written[0], 4)[0]
                return CSW_SIGNATURE + struct.pack("<IIB", tag, 0, status)

        device._ep_out = Out()
        device._ep_in = In()
        return device

    async def test_disconnect_while_reading_ack_is_fine(self):
        from defib.rockusb.recovery import RockchipRecovery

        await RockchipRecovery(self._device(csw_fails=True)).reset()

    async def test_send_failure_propagates(self):
        from defib.rockusb.device import RockusbUsbError
        from defib.rockusb.recovery import RockchipRecovery

        with pytest.raises(RockusbUsbError, match="command wrapper write failed"):
            await RockchipRecovery(self._device(send_fails=True)).reset()

    async def test_explicit_failure_status_propagates(self):
        from defib.rockusb.device import RockusbUsbError
        from defib.rockusb.recovery import RockchipRecovery

        with pytest.raises(RockusbUsbError, match="failed"):
            await RockchipRecovery(self._device(status=1)).reset()


class TestShortControlWrite:
    """A truncated loader upload only shows up much later, as a
    re-enumeration timeout that looks like a dead board."""

    def _device(self, returns):
        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.MASKROM, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        device = RockusbDevice(found)

        class Handle:
            def ctrl_transfer(self, **kwargs):
                return returns(kwargs["data_or_wLength"])

        device._dev = Handle()
        return device

    def test_full_write_returns_the_count(self):
        device = self._device(lambda d: len(d))
        assert device.control_write(0x471, b"\x00" * 100) == 100

    def test_short_write_rejected(self):
        from defib.rockusb.device import RockusbUsbError

        device = self._device(lambda d: len(d) - 1)
        with pytest.raises(RockusbUsbError, match="short"):
            device.control_write(0x471, b"\x00" * 100)

    def test_transfer_error_still_wrapped(self):
        from defib.rockusb.device import RockusbUsbError

        def boom(_data):
            raise OSError("broken pipe")

        with pytest.raises(RockusbUsbError, match="control transfer failed"):
            self._device(boom).control_write(0x471, b"\x00" * 4)


class TestKernelDriverRestore:
    """A failed claim must not strand the interface away from the kernel
    driver that owned it."""

    def _device(self, claim_fails: bool):
        from defib.rockusb.device import RockusbDevice

        found = FoundDevice(
            mode=DeviceMode.LOADER, bus=1, address=1, product_id=0x110C,
            handle=None, port_numbers=(1,),
        )
        device = RockusbDevice(found)
        events: list[str] = []

        class Handle:
            def attach_kernel_driver(self, number):
                events.append(f"attach{number}")

        device._dev = Handle()
        device._detached_interface = 3
        return device, events

    def test_reattach_restores_the_same_interface(self):
        device, events = self._device(claim_fails=True)
        device._reattach_kernel_driver()
        assert events == ["attach3"]

    def test_reattach_is_idempotent(self):
        device, events = self._device(claim_fails=True)
        device._reattach_kernel_driver()
        device._reattach_kernel_driver()
        assert events == ["attach3"]

    def test_nothing_detached_means_nothing_restored(self):
        device, events = self._device(claim_fails=False)
        device._detached_interface = None
        device._reattach_kernel_driver()
        assert events == []


class TestUploadProgress:
    """Progress is measured in framed bytes at both ends.

    Framing appends a CRC and sometimes a terminator packet, so counting real
    bytes sent against the raw blob length reports over 100% — a 3-byte blob
    would announce 5 of 3.
    """

    def test_framed_total_covers_the_crc(self):
        from defib.rockusb.maskrom import build_maskrom_chunks

        blob = b"\x01\x02\x03"
        total = sum(len(c) for c in build_maskrom_chunks(blob))
        assert total == len(blob) + 2
        assert total > len(blob)

    def test_progress_never_exceeds_total(self):
        from defib.rockusb.maskrom import build_maskrom_chunks

        for size in (1, 3, 4094, 4095, 4096, 10000):
            chunks = build_maskrom_chunks(b"\xa5" * size)
            total = sum(len(c) for c in chunks)
            sent = 0
            for c in chunks:
                sent += len(c)
                assert sent <= total
            assert sent == total
