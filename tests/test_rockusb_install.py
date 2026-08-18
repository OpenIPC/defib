"""Tests for USB-recovery tarball handling and device selection.

These cover the guards that stop a flash going wrong in a way the operator
would not notice: a truncated tarball, a corrupted image, an oversized one, a
partial transfer the device reported as OK, or the wrong board entirely.
"""

import hashlib
import io
import tarfile

import pytest
import typer

from defib.cli.app import _read_usb_payloads
from defib.profiles.schema import FlashPartition
from defib.rockusb.device import DeviceMode, FoundDevice
from defib.rockusb.protocol import CSW_SIGNATURE, Opcode

PARTS = {
    "idblock": FlashPartition(lba=512, sectors=512),
    "uboot": FlashPartition(lba=1024, sectors=1024),
    "boot": FlashPartition(lba=2048, sectors=8192),
    "rootfs": FlashPartition(lba=92160, sectors=163840),
}


def _make_tarball(tmp_path, files: dict[str, bytes], *, checksums=True, corrupt=()):
    """Build an OpenIPC-shaped tarball, optionally with bad checksums."""
    path = tmp_path / "fw.tgz"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            if not checksums:
                continue
            digest = hashlib.md5(data).hexdigest()
            if name in corrupt:
                digest = "0" * 32
            line = f"{digest}  {name}\n".encode()
            sig = tarfile.TarInfo(f"{name}.md5sum")
            sig.size = len(line)
            tar.addfile(sig, io.BytesIO(line))
    return path


def _complete(**overrides) -> dict[str, bytes]:
    files = {
        "zboot.img.rv1106": b"\xaa" * 2048,
        "rootfs.squashfs.rv1106": b"\xbb" * 4096,
    }
    files.update(overrides)
    return files


class TestReadUsbPayloads:
    def test_reads_a_complete_tarball(self, tmp_path):
        payloads = _read_usb_payloads(_make_tarball(tmp_path, _complete()), PARTS)
        assert [(n, p) for n, p, _, _ in payloads] == [
            ("zboot.img.rv1106", "boot"),
            ("rootfs.squashfs.rv1106", "rootfs"),
        ]

    def test_returns_image_bytes(self, tmp_path):
        payloads = _read_usb_payloads(_make_tarball(tmp_path, _complete()), PARTS)
        assert dict((n, d) for n, _, _, d in payloads)["zboot.img.rv1106"] == b"\xaa" * 2048

    def test_missing_rootfs_refused(self, tmp_path):
        """Half a firmware written and called a success leaves an unbootable
        board — the UART installer rejects this too."""
        tar = _make_tarball(tmp_path, {"zboot.img.rv1106": b"\xaa" * 16})
        with pytest.raises(typer.BadParameter, match="no image for rootfs"):
            _read_usb_payloads(tar, PARTS)

    def test_missing_kernel_refused(self, tmp_path):
        tar = _make_tarball(tmp_path, {"rootfs.squashfs.rv1106": b"\xbb" * 16})
        with pytest.raises(typer.BadParameter, match="no image for boot"):
            _read_usb_payloads(tar, PARTS)

    def test_empty_tarball_refused(self, tmp_path):
        tar = _make_tarball(tmp_path, {"README": b"nothing here"})
        with pytest.raises(typer.BadParameter, match="maps to a partition"):
            _read_usb_payloads(tar, PARTS)

    def test_idblock_ordered_last(self, tmp_path):
        files = _complete()
        files["idblock.img.rv1106"] = b"\xcc" * 512
        payloads = _read_usb_payloads(_make_tarball(tmp_path, files), PARTS)
        assert [p for _, p, _, _ in payloads][-1] == "idblock"


class TestChecksums:
    def test_corrupted_image_refused(self, tmp_path):
        """--verify compares flash against what was sent, so it cannot catch a
        bad download; only the shipped md5sum can."""
        tar = _make_tarball(tmp_path, _complete(), corrupt=("zboot.img.rv1106",))
        with pytest.raises(typer.BadParameter, match="MD5 mismatch"):
            _read_usb_payloads(tar, PARTS)

    def test_error_names_both_digests(self, tmp_path):
        tar = _make_tarball(tmp_path, _complete(), corrupt=("zboot.img.rv1106",))
        with pytest.raises(typer.BadParameter) as excinfo:
            _read_usb_payloads(tar, PARTS)
        message = str(excinfo.value)
        assert "0" * 32 in message
        assert hashlib.md5(b"\xaa" * 2048).hexdigest() in message

    def test_tarball_without_checksums_still_works(self, tmp_path):
        """Absent checksums are not an error — only mismatching ones are."""
        tar = _make_tarball(tmp_path, _complete(), checksums=False)
        assert len(_read_usb_payloads(tar, PARTS)) == 2


class TestBounds:
    def test_oversized_image_refused(self, tmp_path):
        files = _complete(**{"zboot.img.rv1106": b"\xaa" * (PARTS["boot"].size_bytes + 1)})
        with pytest.raises(typer.BadParameter, match="would overwrite"):
            _read_usb_payloads(_make_tarball(tmp_path, files), PARTS)

    def test_exactly_full_partition_accepted(self, tmp_path):
        files = _complete(**{"zboot.img.rv1106": b"\xaa" * PARTS["boot"].size_bytes})
        assert len(_read_usb_payloads(_make_tarball(tmp_path, files), PARTS)) == 2


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
