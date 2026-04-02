"""Tests for shared serial port discovery and formatting."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from defib import serial_ports


def _fake_port(
    *,
    device: str,
    description: str = "FT232R USB UART - FT232R USB UART",
    manufacturer: str | None = "FTDI",
    product: str | None = "FT232R USB UART",
    serial_number: str | None = "A50285BI",
    location: str | None = "1-4",
    vid: int | None = 0x0403,
    pid: int | None = 0x6001,
    hwid: str = "USB VID:PID=0403:6001 SER=A50285BI LOCATION=1-4",
) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description=description,
        manufacturer=manufacturer,
        product=product,
        serial_number=serial_number,
        location=location,
        vid=vid,
        pid=pid,
        hwid=hwid,
    )


def test_list_serial_ports_without_alias(monkeypatch):
    monkeypatch.setattr(serial_ports, "comports", lambda: [_fake_port(device="/dev/ttyUSB0")])
    monkeypatch.setattr(serial_ports, "_discover_aliases", lambda: {})

    ports = serial_ports.list_serial_ports()

    assert len(ports) == 1
    assert ports[0].open_path == "/dev/ttyUSB0"
    assert ports[0].alias_device is None
    assert ports[0].display_name == "/dev/ttyUSB0 | FTDI FT232R USB UART | loc 1-4 | ser A50285BI"


def test_list_serial_ports_prefers_uart_alias(monkeypatch):
    monkeypatch.setattr(serial_ports, "comports", lambda: [_fake_port(device="/dev/ttyUSB1", location="5-2")])
    monkeypatch.setattr(
        serial_ports,
        "_discover_aliases",
        lambda: {
            "/dev/ttyUSB1": [
                "/dev/serial/by-path/pci-0000:0e:00.0-usb-0:2:1.0-port0",
                "/dev/uart-orangepi5plus",
                "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
            ]
        },
    )

    ports = serial_ports.list_serial_ports()

    assert len(ports) == 1
    assert ports[0].alias_device == "/dev/uart-orangepi5plus"
    assert ports[0].open_path == "/dev/uart-orangepi5plus"
    assert ports[0].display_name.startswith("/dev/uart-orangepi5plus -> /dev/ttyUSB1")
    assert "loc 5-2" in ports[0].display_name


def test_preferred_alias_priority():
    aliases = [
        "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
        "/dev/serial/by-path/pci-0000:0e:00.0-usb-0:2:1.0-port0",
        "/dev/uart-orangepi5plus",
    ]

    assert serial_ports._preferred_alias(aliases) == "/dev/uart-orangepi5plus"


def test_discover_aliases_collects_symlinks(monkeypatch, tmp_path):
    device = tmp_path / "ttyUSB0"
    device.write_text("")
    uart_alias = tmp_path / "uart-orangepi5plus"
    by_id_alias = tmp_path / "usb-ftdi"
    uart_alias.symlink_to(device)
    by_id_alias.symlink_to(device)

    original_glob = Path.glob

    def fake_glob(self: Path, pattern: str):
        if self == Path("/") and pattern == "dev/uart-*":
            return [uart_alias]
        if self == Path("/") and pattern == "dev/serial/by-id/*":
            return [by_id_alias]
        if self == Path("/") and pattern == "dev/serial/by-path/*":
            return []
        return original_glob(self, pattern)

    monkeypatch.setattr(serial_ports.sys, "platform", "linux")
    monkeypatch.setattr(Path, "glob", fake_glob)

    aliases = serial_ports._discover_aliases()

    assert aliases[str(device)] == [str(uart_alias), str(by_id_alias)]
