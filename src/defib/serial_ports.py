"""Shared serial port discovery and display formatting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from serial.tools.list_ports import comports


@dataclass(frozen=True)
class SerialPortInfo:
    """Normalized serial port metadata for CLI and TUI surfaces."""

    device: str
    open_path: str
    display_name: str
    alias_device: str | None
    description: str
    manufacturer: str | None
    product: str | None
    serial_number: str | None
    location: str | None
    vid: int | None
    pid: int | None
    hwid: str


def _discover_aliases() -> dict[str, list[str]]:
    """Return symlink aliases keyed by resolved device path."""
    alias_map: dict[str, list[str]] = {}

    if sys.platform == "win32":
        return alias_map

    alias_patterns = (
        "/dev/uart-*",
        "/dev/serial/by-id/*",
        "/dev/serial/by-path/*",
    )

    for pattern in alias_patterns:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            try:
                if not path.is_symlink():
                    continue
                target = str(path.resolve())
            except OSError:
                continue
            alias_map.setdefault(target, []).append(str(path))

    return alias_map


def _alias_priority(alias: str) -> tuple[int, str]:
    if alias.startswith("/dev/uart-"):
        return (0, alias)
    if alias.startswith("/dev/serial/by-id/"):
        return (1, alias)
    if alias.startswith("/dev/serial/by-path/"):
        return (2, alias)
    return (3, alias)


def _preferred_alias(aliases: list[str]) -> str | None:
    if not aliases:
        return None
    return sorted(aliases, key=_alias_priority)[0]


def _port_identity(manufacturer: str | None, product: str | None, description: str) -> str:
    parts = [part for part in (manufacturer, product) if part]
    if parts:
        return " ".join(parts)
    return description or "Unknown serial adapter"


def _port_display_name(
    *,
    alias_device: str | None,
    device: str,
    manufacturer: str | None,
    product: str | None,
    description: str,
    location: str | None,
    serial_number: str | None,
) -> str:
    segments = []
    if alias_device:
        segments.append(f"{alias_device} -> {device}")
    else:
        segments.append(device)

    segments.append(_port_identity(manufacturer, product, description))

    if location:
        segments.append(f"loc {location}")
    if serial_number:
        segments.append(f"ser {serial_number}")

    return " | ".join(segments)


def _coerce_attr(port: Any, name: str) -> Any:
    return getattr(port, name, None)


def list_serial_ports() -> list[SerialPortInfo]:
    """List USB serial adapters with stable display metadata."""
    alias_map = _discover_aliases()
    ports = sorted([p for p in comports() if p.vid is not None], key=lambda p: p.device)

    entries: list[SerialPortInfo] = []
    for port in ports:
        device = str(port.device)
        alias_device = _preferred_alias(alias_map.get(device, []))
        description = str(port.description)
        manufacturer = _coerce_attr(port, "manufacturer")
        product = _coerce_attr(port, "product")
        serial_number = _coerce_attr(port, "serial_number")
        location = _coerce_attr(port, "location")
        vid = _coerce_attr(port, "vid")
        pid = _coerce_attr(port, "pid")
        hwid = str(port.hwid)

        entries.append(
            SerialPortInfo(
                device=device,
                open_path=alias_device or device,
                display_name=_port_display_name(
                    alias_device=alias_device,
                    device=device,
                    manufacturer=manufacturer,
                    product=product,
                    description=description,
                    location=location,
                    serial_number=serial_number,
                ),
                alias_device=alias_device,
                description=description,
                manufacturer=manufacturer,
                product=product,
                serial_number=serial_number,
                location=location,
                vid=vid,
                pid=pid,
                hwid=hwid,
            )
        )

    return entries
