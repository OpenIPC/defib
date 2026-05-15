"""Firmware auto-download from OpenIPC releases.

Downloads pre-built U-Boot binaries from the OpenIPC firmware repository.
URL pattern: https://github.com/OpenIPC/firmware/releases/download/latest/u-boot-{chip}-universal.bin

Caches downloads in a platform-appropriate directory to avoid re-downloading.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

OPENIPC_BASE_URL = (
    "https://github.com/OpenIPC/firmware/releases/download/latest"
)

# Chips that have pre-built u-boot binaries on OpenIPC
AVAILABLE_FIRMWARE: set[str] = {
    "gk7202v300", "gk7205v200", "gk7205v300", "gk7605v100",
    "hi3516av100", "hi3516av200", "hi3516av300",
    "hi3516cv100", "hi3516cv200", "hi3516cv300", "hi3516cv500",
    "hi3516dv100", "hi3516dv200", "hi3516dv300",
    "hi3516ev100", "hi3516ev200", "hi3516ev300",
    "hi3518av100", "hi3518cv100", "hi3518ev100", "hi3518ev200", "hi3518ev300",
    "hi3519v101",
    "t40a", "t40n", "t40xp",
}

# Chip aliases: map chip names to the firmware download name
# e.g. hi3516ev300 profile resolves to hi3516ev200 internally,
# but the firmware binary is named u-boot-hi3516ev300-universal.bin
CHIP_TO_FIRMWARE: dict[str, str] = {
    "hi3518ev201": "hi3518ev200",
    "hi3516dv100": "hi3516dv100",
    "gk7201v200": "gk7205v200",
    "gk7201v300": "gk7205v200",
    "gk7205v210": "gk7205v200",
}


def get_cache_dir() -> Path:
    """Get platform-appropriate cache directory for downloaded firmware."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache_dir = base / "defib" / "firmware"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _strip_variant(chip: str) -> str:
    """Drop the optional ``:variant`` suffix — U-Boot binaries are per-chip,
    not per-board, so any variant suffix is irrelevant for firmware lookup."""
    return chip.split(":", 1)[0]


def firmware_url(chip: str) -> str | None:
    """Get the OpenIPC download URL for a chip, or None if unavailable."""
    chip = _strip_variant(chip)
    name = CHIP_TO_FIRMWARE.get(chip, chip)
    if name in AVAILABLE_FIRMWARE:
        return f"{OPENIPC_BASE_URL}/u-boot-{name}-universal.bin"
    return None


def has_firmware(chip: str) -> bool:
    """Check if OpenIPC has a pre-built firmware for this chip."""
    return firmware_url(chip) is not None


def get_cached_path(chip: str) -> Path | None:
    """Get the path to cached firmware, or None if not cached."""
    chip = _strip_variant(chip)
    name = CHIP_TO_FIRMWARE.get(chip, chip)
    path = get_cache_dir() / f"u-boot-{name}-universal.bin"
    if path.exists() and path.stat().st_size > 0:
        return path
    return None


def pad_to_size(data: bytes, target_size: int, fill: int = 0xFF) -> bytes:
    """Right-pad `data` to `target_size` with `fill` bytes (default 0xFF).

    OpenIPC dropped the historical 1 MiB padding from published U-Boot
    assets (issue #73). Consumers that flash to a fixed-size partition
    must now pad locally to the partition size so trailing flash bytes
    are erased (0xFF), not left at whatever was previously written.
    """
    if len(data) > target_size:
        raise ValueError(
            f"Data is {len(data)} bytes, larger than target {target_size}"
        )
    return data.ljust(target_size, bytes([fill]))


def download_firmware(
    chip: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download U-Boot firmware from OpenIPC, with caching.

    Args:
        chip: Chip name (e.g., "hi3516ev300").
        on_progress: Optional callback(bytes_downloaded, total_bytes).

    Returns:
        Path to the downloaded (or cached) firmware file.

    Raises:
        ValueError: If no firmware is available for this chip.
        ConnectionError: If download fails.
    """
    url = firmware_url(chip)
    if url is None:
        raise ValueError(
            f"No pre-built firmware available for '{chip}'. "
            f"Use -f/--file to specify a local firmware file."
        )

    # Check cache
    cached = get_cached_path(chip)
    if cached is not None:
        logger.info("Using cached firmware: %s", cached)
        return cached

    # Download
    chip = _strip_variant(chip)
    name = CHIP_TO_FIRMWARE.get(chip, chip)
    dest = get_cache_dir() / f"u-boot-{name}-universal.bin"
    logger.info("Downloading firmware from %s", url)

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "defib/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            data = bytearray()
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if on_progress and total:
                    on_progress(len(data), total)

        if len(data) < 1024:
            raise ConnectionError(f"Download too small ({len(data)} bytes)")

        dest.write_bytes(data)
        logger.info("Downloaded %d bytes to %s", len(data), dest)
        return dest

    except Exception as e:
        # Clean up partial download
        if dest.exists():
            dest.unlink()
        if isinstance(e, (ValueError, ConnectionError)):
            raise
        raise ConnectionError(f"Failed to download firmware: {e}") from e
