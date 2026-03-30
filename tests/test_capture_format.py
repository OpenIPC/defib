"""Tests for the .dcap capture format."""

import struct
from pathlib import Path

import pytest

from defib.capture.format import (
    CaptureFile,
    Direction,
    DCAP_MAGIC,
    DCAP_VERSION,
    HEADER_SIZE,
)


class TestCaptureFile:
    def test_create_empty(self):
        cap = CaptureFile()
        assert cap.baudrate == 115200
        assert cap.chip == ""
        assert len(cap.records) == 0

    def test_add_tx_rx(self):
        cap = CaptureFile(chip="hi3516cv300")
        cap.add_tx(0, b"\xaa")
        cap.add_rx(1000, b"\x20\x20\x20")
        assert len(cap.records) == 2
        assert cap.records[0].direction == Direction.TX
        assert cap.records[1].direction == Direction.RX

    def test_tx_rx_bytes(self):
        cap = CaptureFile()
        cap.add_tx(0, b"\x01\x02\x03")
        cap.add_rx(100, b"\xaa")
        cap.add_tx(200, b"\x04\x05")
        assert cap.tx_bytes == 5
        assert cap.rx_bytes == 1

    def test_duration(self):
        cap = CaptureFile()
        cap.add_tx(1000, b"\x01")
        cap.add_rx(5000, b"\x02")
        assert cap.duration_us == 4000

    def test_duration_empty(self):
        cap = CaptureFile()
        assert cap.duration_us == 0


class TestCaptureEncodeDecode:
    def test_header_size(self):
        cap = CaptureFile()
        data = cap.encode()
        assert len(data) >= HEADER_SIZE

    def test_header_magic(self):
        cap = CaptureFile()
        data = cap.encode()
        assert data[:4] == DCAP_MAGIC

    def test_header_version(self):
        cap = CaptureFile()
        data = cap.encode()
        version = struct.unpack_from("<H", data, 4)[0]
        assert version == DCAP_VERSION

    def test_header_baudrate(self):
        cap = CaptureFile(baudrate=9600)
        data = cap.encode()
        baudrate = struct.unpack_from("<I", data, 6)[0]
        assert baudrate == 9600

    def test_roundtrip_empty(self):
        original = CaptureFile(baudrate=115200, chip="test_chip")
        encoded = original.encode()
        decoded = CaptureFile.decode(encoded)
        assert decoded.baudrate == 115200
        assert decoded.chip == "test_chip"
        assert len(decoded.records) == 0

    def test_roundtrip_with_records(self):
        original = CaptureFile(chip="hi3516ev300")
        original.add_tx(0, b"\xfe\x00\xff\x01")
        original.add_rx(500, b"\xaa")
        original.add_tx(1000, b"\xda\x01\xfe" + bytes(range(100)))
        original.add_rx(2000, b"\xaa")

        encoded = original.encode()
        decoded = CaptureFile.decode(encoded)

        assert decoded.chip == "hi3516ev300"
        assert len(decoded.records) == 4
        assert decoded.records[0].direction == Direction.TX
        assert decoded.records[0].data == b"\xfe\x00\xff\x01"
        assert decoded.records[0].timestamp_us == 0
        assert decoded.records[1].direction == Direction.RX
        assert decoded.records[1].data == b"\xaa"
        assert decoded.records[1].timestamp_us == 500
        assert decoded.records[2].data == b"\xda\x01\xfe" + bytes(range(100))
        assert decoded.records[3].timestamp_us == 2000

    def test_roundtrip_large_payload(self):
        original = CaptureFile()
        big_data = bytes(range(256)) * 10  # 2560 bytes
        original.add_tx(0, big_data)
        encoded = original.encode()
        decoded = CaptureFile.decode(encoded)
        assert decoded.records[0].data == big_data

    def test_chip_name_truncation(self):
        """Chip name longer than 32 bytes should be truncated."""
        original = CaptureFile(chip="a" * 100)
        encoded = original.encode()
        decoded = CaptureFile.decode(encoded)
        assert len(decoded.chip) <= 32

    def test_decode_invalid_magic(self):
        data = b"XXXX" + b"\x00" * 44
        with pytest.raises(ValueError, match="Invalid magic"):
            CaptureFile.decode(data)

    def test_decode_too_small(self):
        with pytest.raises(ValueError, match="too small"):
            CaptureFile.decode(b"\x00" * 10)


class TestCaptureFileIO:
    def test_save_and_load(self, tmp_path: Path):
        cap = CaptureFile(chip="gk7205v200", baudrate=115200)
        cap.add_tx(0, b"\xbd\x00\xff\x01")
        cap.add_rx(100, b"\xbd\x00" + b"\x00" * 12)

        path = tmp_path / "test.dcap"
        cap.save(path)

        loaded = CaptureFile.load(path)
        assert loaded.chip == "gk7205v200"
        assert len(loaded.records) == 2
        assert loaded.records[0].data == b"\xbd\x00\xff\x01"


class TestDirection:
    def test_tx_value(self):
        assert Direction.TX == 0

    def test_rx_value(self):
        assert Direction.RX == 1
