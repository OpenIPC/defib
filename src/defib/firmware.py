"""Firmware auto-download from OpenIPC releases.

Downloads pre-built U-Boot binaries from the OpenIPC firmware repository.
Two asset families are published, and which one applies depends on the SoC:

- Classic SoCs: ``u-boot-{chip}-universal.bin`` — a bare U-Boot image.
- CV6xx SoCs: ``boot-{chip}[-{variant}]-nor.bin`` — a composite image
  (GSL + DDR tables + U-Boot) that the bootrom expects as a single blob.

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

# Chips that have pre-built u-boot-{chip}-universal.bin binaries on OpenIPC.
# CV6xx chips are deliberately absent — see CV6XX_BOOT_VARIANTS below.
AVAILABLE_FIRMWARE: set[str] = {
    "gk7202v300", "gk7205v200", "gk7205v300", "gk7605v100",
    "hi3516av100", "hi3516av200", "hi3516av300",
    "hi3516cv100", "hi3516cv200", "hi3516cv300", "hi3516cv500",
    "hi3516dv100", "hi3516dv200", "hi3516dv300",
    "hi3516ev100", "hi3516ev200", "hi3516ev300",
    "hi3518av100", "hi3518cv100", "hi3518ev100", "hi3518ev200", "hi3518ev300",
    "hi3519v101", "hi3520dv200", "hi3536cv100", "hi3536dv100",
    "t40a", "t40n", "t40xp",
}

# CV6xx SoCs publish a composite boot image, not a bare U-Boot, and no
# u-boot-{chip}-universal.bin exists for any of them — the URL assumed by
# #112 always 404'd, so auto-download could never work and users had to
# hand-seed the cache. The real assets are boot-{chip}[-{variant}]-nor.bin.
#
# Variants are board-specific: across the five hi3516cv610 images the GSL is
# identical but each carries a different U-Boot, and the DDR table differs by
# the numeric prefix (00g/00s share one, 10b another, 20g/20s a third). Nothing
# in the recovery handshake identifies the board, so a variant cannot be
# guessed — a chip with more than one published image must be requested
# explicitly as "chip:variant" (e.g. hi3516cv610:00g).
CV6XX_BOOT_VARIANTS: dict[str, tuple[str, ...]] = {
    "hi3516cv608": (),  # single image, no variant suffix
    "hi3516cv610": ("00g", "00s", "10b", "20g", "20s"),
    "hi3519dv500": ("dmeb", "dmebpro"),
    # hi3516cv613 and hi3516dv500 have no published boot image yet.
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


def get_cache_dir(create: bool = True) -> Path:
    """Get platform-appropriate cache directory for downloaded firmware.

    `create=False` just names the directory. Callers that are only searching
    should use it: an unwritable cache location must not stop a lookup that
    would have found the file somewhere else entirely.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cache_dir = base / "defib" / "firmware"
    if create:
        cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _split_variant(chip: str) -> tuple[str, str | None]:
    """Split ``chip[:variant]`` into its parts."""
    base, _, variant = chip.partition(":")
    return base, variant or None


def _strip_variant(chip: str) -> str:
    """Drop the optional ``:variant`` suffix. For classic SoCs the U-Boot
    binary is per-chip, not per-board, so the suffix is irrelevant; CV6xx is
    the exception and is resolved by :func:`asset_name` before this is used."""
    return _split_variant(chip)[0]


def asset_name(chip: str) -> str | None:
    """Published release filename for a chip, or None if there isn't one.

    Returns None both for chips OpenIPC doesn't build and for a CV6xx chip
    named without the variant needed to pick between several board images.
    """
    base, variant = _split_variant(chip)
    name = CHIP_TO_FIRMWARE.get(base, base)

    if name in CV6XX_BOOT_VARIANTS:
        variants = CV6XX_BOOT_VARIANTS[name]
        if not variants:
            # Only one image published; any variant suffix is not meaningful.
            return f"boot-{name}-nor.bin"
        if variant in variants:
            return f"boot-{name}-{variant}-nor.bin"
        return None

    if name in AVAILABLE_FIRMWARE:
        return f"u-boot-{name}-universal.bin"
    return None


def firmware_url(chip: str) -> str | None:
    """Get the OpenIPC download URL for a chip, or None if unavailable."""
    name = asset_name(chip)
    return f"{OPENIPC_BASE_URL}/{name}" if name else None


def has_firmware(chip: str) -> bool:
    """Check if firmware can be obtained for this chip.

    True when OpenIPC publishes an image *or* one is already cached — the
    latter keeps hand-seeded blobs working for chips with no published build.
    """
    return firmware_url(chip) is not None or get_cached_path(chip) is not None


def _legacy_cache_name(chip: str) -> str:
    """Historic cache filename, which assumed every chip used the universal
    naming. CV6xx users were told to seed the cache under this name by #112,
    so keep reading it rather than silently re-downloading over it."""
    base = _strip_variant(chip)
    return f"u-boot-{CHIP_TO_FIRMWARE.get(base, base)}-universal.bin"


def get_cached_path(chip: str) -> Path | None:
    """Get the path to cached firmware, or None if not cached."""
    cache_dir = get_cache_dir()
    candidates = [asset_name(chip), _legacy_cache_name(chip)]
    for candidate in candidates:
        if not candidate:
            continue
        path = cache_dir / candidate
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


def _unavailable_message(chip: str) -> str:
    """Explain why a chip can't be resolved, naming the variants when the
    chip is a CV6xx whose board image is ambiguous."""
    base, _ = _split_variant(chip)
    name = CHIP_TO_FIRMWARE.get(base, base)
    variants = CV6XX_BOOT_VARIANTS.get(name)
    if variants:
        return (
            f"'{chip}' needs an explicit board variant — OpenIPC publishes "
            f"{len(variants)} different boot images for {name} and they carry "
            f"different DDR settings, so the right one cannot be guessed. "
            f"Use one of: " + ", ".join(f"{name}:{v}" for v in variants)
            + ". Or use -f/--file to specify a local firmware file."
        )
    return (
        f"No pre-built firmware available for '{chip}'. "
        f"Use -f/--file to specify a local firmware file."
    )


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
        # Check the cache before giving up: a chip with no published build may
        # still have a hand-seeded blob.
        cached = get_cached_path(chip)
        if cached is not None:
            logger.info("Using cached firmware: %s", cached)
            return cached
        raise ValueError(_unavailable_message(chip))

    # Check cache
    cached = get_cached_path(chip)
    if cached is not None:
        logger.info("Using cached firmware: %s", cached)
        return cached

    # Download
    name = asset_name(chip)
    assert name is not None  # firmware_url() is None otherwise
    dest = get_cache_dir() / name
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
