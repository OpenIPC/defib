"""Flash layouts and U-Boot helpers shared by install orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
import zlib
from collections.abc import Awaitable, Callable

from defib.uboot_env import parse_printenv_value

Command = Callable[..., Awaitable[str]]
logger = logging.getLogger(__name__)


NOR8M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x200000),
    "rootfs": (0x250000, 0x500000),
}

NOR16M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x300000),
    "rootfs": (0x350000, 0xA00000),
}

NOR32M_LAYOUT = {
    "boot": (0x000000, 0x40000),
    "env": (0x040000, 0x10000),
    "kernel": (0x050000, 0x300000),
    "rootfs": (0x350000, 0x1800000),
}

NAND_LAYOUT = {
    "boot": (0x000000, 0x100000),
    "env": (0x100000, 0x100000),
    "kernel": (0x200000, 0x800000),
    "rootfs": (0xA00000, 0x7600000),
}


def align_up(value: int, alignment: int) -> int:
    if value < 0:
        raise ValueError("value must be non-negative")
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return ((value + alignment - 1) // alignment) * alignment


def erased_region_crc(size: int) -> int:
    if size < 0:
        raise ValueError("size must be non-negative")
    crc = 0
    chunk = b"\xff" * min(size, 0x10000)
    remaining = size
    while remaining:
        piece = chunk if remaining >= len(chunk) else chunk[:remaining]
        crc = zlib.crc32(piece, crc)
        remaining -= len(piece)
    return crc & 0xFFFFFFFF


def uboot_flash_command_error(response: str) -> str | None:
    """Return a flash-command failure marker from command result lines.

    U-Boot responses can include command echo and unrelated banner text.  Do
    not treat an arbitrary ``failed`` substring anywhere in that buffer as the
    result of the destructive command that just ran.
    """
    result_prefixes = (
        "sf:",
        "spi flash",
        "spi nor",
        "nand",
        "erase",
        "erasing",
        "write",
        "writing",
        "read",
        "reading",
    )
    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        text = line.lower()
        if text.startswith(("error:", "unknown command", "usage:")):
            return line
        if re.match(r"failed\b", text):
            return line
        if "no spi flash selected" in text:
            return line
        if "out of range" in text or "not block aligned" in text:
            return line
        if text in {"failed", "failure"}:
            return line
        if text.startswith(result_prefixes) and re.search(r"\b(?:failed|failure)\b", text):
            return line
    return None


def uboot_sf_lock_unsupported(response: str) -> bool:
    """Return True only when the U-Boot build lacks a usable sf lock command."""

    lines = [line.strip().lower() for line in response.splitlines() if line.strip()]
    for line in lines:
        mentions_sf_lock = "sf" in line or "lock" in line
        if mentions_sf_lock and "unknown command" in line:
            return True
        if mentions_sf_lock and ("not supported" in line or "unsupported" in line):
            return True

    # Older U-Boot variants often print the whole sf usage table for an unknown
    # subcommand. That is compatibility information only when the table itself
    # contains no sf lock entry. If sf lock is listed, a Usage response means our
    # invocation was rejected and must remain a hard failure.
    usage_seen = any(line.startswith("usage:") for line in lines)
    sf_usage: list[str] = []
    for line in lines:
        candidate = line.removeprefix("usage:").strip() if line.startswith("usage:") else line
        if re.match(r"^sf(?:\s|$)", candidate):
            sf_usage.append(candidate)
    if usage_seen and sf_usage:
        return not any(re.match(r"^sf\s+lock(?:\s|$)", line) for line in sf_usage)

    return False


async def set_uboot_env_verified(
    cmd: Command,
    key: str,
    value: str,
    *,
    attempts: int = 3,
) -> None:
    """Set a transient U-Boot value and verify it from the live shell."""
    last_actual: str | None = None
    last_response = ""
    for attempt in range(max(1, attempts)):
        set_response = await cmd(f"setenv {key} {value}", timeout=3.0)
        last_response = set_response
        if uboot_flash_command_error(set_response) is None:
            verify_response = await cmd(f"printenv {key}", timeout=3.0)
            last_response = verify_response
            last_actual = parse_printenv_value(verify_response, key)
            if last_actual == value:
                return
        if attempt + 1 < max(1, attempts):
            await asyncio.sleep(0.05)

    raise RuntimeError(
        f"U-Boot runtime environment verify failed for {key}: "
        f"expected={value!r} got={last_actual!r}; "
        f"response={last_response.strip()[-160:]!r}"
    )


def nor_mtdparts(nor_size: int) -> str:
    if nor_size >= 32:
        return (
            "hi_sfc:256k(boot),64k(env),3072k(kernel),"
            "24576k(rootfs),-(rootfs_data)"
        )
    if nor_size >= 16:
        return (
            "hi_sfc:256k(boot),64k(env),3072k(kernel),"
            "10240k(rootfs),-(rootfs_data)"
        )
    return (
        "hi_sfc:256k(boot),64k(env),2048k(kernel),"
        "5120k(rootfs),-(rootfs_data)"
    )


def nand_bootargs(rootfs_is_ubi: bool) -> str:
    base = (
        "mem=256M console=ttyAMA0,115200 panic=20 ubi.mtd=3,2048 "
        "mtdparts=hinand:1024k(boot),1024k(env),8192k(kernel),-(ubi)"
    )
    if rootfs_is_ubi:
        return f"root=ubi0:rootfs rootfstype=ubifs {base}"
    return (
        "root=/dev/ubiblock0_0 rootfstype=squashfs ubi.block=0,0 "
        f"init=/init {base}"
    )


def parse_uboot_crc32(response: str) -> int | None:
    """Return the CRC printed by U-Boot, or ``None`` if it is missing."""
    match = re.search(r"==>\s*([0-9a-fA-F]{8})", response)
    return int(match.group(1), 16) if match else None


async def verify_spi_environment_crc(
    cmd: Command,
    *,
    env_off: int,
    env_size: int,
    ram_addr: int,
) -> int:
    """Read the saved SPI environment back and validate its on-flash CRC.

    OpenIPC's non-redundant SPI environment is ``uint32_t crc`` followed by
    ``CONFIG_ENV_SIZE - 4`` data bytes.  U-Boot stores both the header CRC and
    the value produced by the ``crc32 ... <storeaddr>`` command in native word
    order, so ``cmp.l`` validates them without host-endianness assumptions.
    """
    if env_size <= 4:
        raise ValueError("environment partition is too small for a CRC header")

    # ``reset`` clears U-Boot's in-memory SPI selection state.  Environment
    # installation deliberately performs an internal reset after erasing the
    # old environment, so always re-probe immediately before physical
    # readback instead of relying on an earlier installer-stage probe.
    probe_response = await cmd("sf probe 0", timeout=10.0)
    logger.debug("SPI env probe response: %r", probe_response)
    probe_error = uboot_flash_command_error(probe_response)
    if probe_error:
        raise RuntimeError(f"environment SPI probe failed: {probe_error}")

    read_response = await cmd(
        f"sf read 0x{ram_addr:x} 0x{env_off:x} 0x{env_size:x}",
        timeout=30.0,
    )
    logger.debug("SPI env readback response: %r", read_response)
    read_error = uboot_flash_command_error(read_response)
    if read_error:
        raise RuntimeError(f"environment readback failed: {read_error}")

    crc_store_addr = ram_addr + env_size
    crc_response = await cmd(
        f"crc32 0x{ram_addr + 4:x} 0x{env_size - 4:x} 0x{crc_store_addr:x}",
        timeout=10.0,
    )
    logger.debug("SPI env data CRC response: %r", crc_response)
    data_crc = parse_uboot_crc32(crc_response)
    if data_crc is None:
        raise RuntimeError("could not parse persisted environment data CRC")

    compare_response = await cmd(
        f"cmp.l 0x{ram_addr:x} 0x{crc_store_addr:x} 1",
        timeout=5.0,
    )
    logger.debug("SPI env CRC compare response: %r", compare_response)
    compare_error = uboot_flash_command_error(compare_response)
    if compare_error:
        raise RuntimeError(f"environment CRC compare failed: {compare_error}")
    if not re.search(
        r"\bTotal of\s+1\s+word(?:\(s\)|s)?\s+were the same\b",
        compare_response,
        re.IGNORECASE,
    ):
        raise RuntimeError(
            "persisted U-Boot environment failed its on-flash CRC check; "
            f"cmp response={compare_response.strip()[-240:]!r}"
        )
    return data_crc


def detect_nor_size_mb(response: str) -> int | None:
    """Parse SPI NOR capacity from common HiSilicon U-Boot ``sf probe`` output."""
    patterns = (
        (r"\bChip:\s*(\d+)\s*MB\b", 1),
        (r"\bspi\s+size:\s*(\d+)\s*MB\b", 1),
        (r"\b(?:spi\s+nor\s+)?total\s+size:\s*(\d+)\s*MB\b", 1),
        (r"\bSF:[^\n]*\btotal\s+(\d+)\s*MB\b", 1),
        (r"\b(\d+)\s+MiB\b[^\n]*(?:hi_sfc|spi)", 1),
        (r"\b(\d+)\s+KiB\b[^\n]*(?:hi_sfc|spi)", 1024),
    )
    for pattern, divisor in patterns:
        match = re.search(pattern, response, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if divisor == 1:
                return value
            if value % divisor == 0:
                return value // divisor
    return None


def select_nor_size_mb(
    override: int,
    detected: int | None,
    *,
    require_detection: bool,
) -> tuple[int, str]:
    """Choose NOR capacity while preserving ``--nor-size`` as an override."""
    if override:
        return override, "override"
    if detected is not None:
        return detected, "detected"
    if require_detection:
        raise ValueError(
            "could not detect NOR capacity from U-Boot; specify --nor-size explicitly"
        )
    return 8, "fallback"


def nor_layout(nor_size_mb: int) -> dict[str, tuple[int, int]]:
    """Return the standard OpenIPC NOR partition layout for a flash size."""
    if nor_size_mb >= 32:
        return NOR32M_LAYOUT
    if nor_size_mb >= 16:
        return NOR16M_LAYOUT
    return NOR8M_LAYOUT
