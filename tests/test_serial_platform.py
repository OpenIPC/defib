"""Tests for platform-specific serial transport functionality."""

import sys

import pytest

from defib.transport.serial_platform import normalize_port_name


class TestNormalizePortName:
    def test_linux_passthrough(self):
        assert normalize_port_name("/dev/ttyUSB0") == "/dev/ttyUSB0"
        assert normalize_port_name("/dev/ttyACM0") == "/dev/ttyACM0"

    def test_macos_passthrough(self):
        assert normalize_port_name("/dev/cu.usbserial-1420") == "/dev/cu.usbserial-1420"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_high_com_port(self):
        result = normalize_port_name("COM10")
        assert result == "\\\\.\\COM10"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_low_com_port(self):
        result = normalize_port_name("COM3")
        assert result == "COM3"
