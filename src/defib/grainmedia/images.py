"""Which bytes go where on an OpenIPC GM8126 NOR flash."""

from __future__ import annotations

from defib.grainmedia.protocol import (
    ERASE_SIZE,
    HEADER_MAGIC,
    GrainMediaError,
    has_system_header,
    validate_boot_image,
)
from defib.grainmedia.recovery import Segment
from defib.install.firmware import SQUASHFS_MAGIC, UIMAGE_MAGIC, FirmwareBundle
from defib.profiles.schema import SoCProfile

#: Stages that need nothing from the firmware archive.
FIRMWARE_STAGES = frozenset({"kernel", "rootfs"})


def _area(profile: SoCProfile, name: str) -> tuple[int, int]:
    extent = profile.partitions[name]
    return extent.lba * 512, extent.size_bytes


def _fit(profile: SoCProfile, name: str, data: bytes) -> Segment:
    offset, size = _area(profile, name)
    if len(data) > size:
        raise GrainMediaError(f"{name} is {len(data)} bytes, its partition is {size}")
    return Segment(name, offset, data.ljust(size, b"\xff"))


def check_firmware(bundle: FirmwareBundle, chip: str) -> None:
    """Refuse another SoC's archive: writing it costs a full reinstall."""
    if bundle.split:
        raise GrainMediaError(f"{chip} does not use the split NOR layout (firmware.bin)")
    for name in (bundle.kernel_name, bundle.rootfs_name):
        if not name.endswith(f".{chip}"):
            raise GrainMediaError(f"{name} is not built for {chip}")
    if not bundle.kernel.startswith(UIMAGE_MAGIC):
        raise GrainMediaError(f"{bundle.kernel_name} is not a uImage")
    if not bundle.rootfs.startswith(SQUASHFS_MAGIC):
        raise GrainMediaError(f"{bundle.rootfs_name} is not a squashfs")


def install_segments(
    profile: SoCProfile,
    stages: set[str],
    flash_size: int,
    *,
    bundle: FirmwareBundle | None,
    uboot: bytes | None,
    wipe_env: bool,
    wipe_rootfs_data: bool,
) -> list[Segment]:
    """The flash contents an install writes, for the stages selected."""
    out: list[Segment] = []
    rootfs_end = sum(_area(profile, "rootfs"))
    if flash_size < rootfs_end + ERASE_SIZE:
        raise GrainMediaError(
            f"the OpenIPC layout needs more than {rootfs_end:#x} bytes of flash, "
            f"this chip has {flash_size:#x}"
        )
    if "uboot" in stages:
        assert uboot is not None
        validate_boot_image(uboot)
        out.append(_fit(profile, "boot", uboot))
    if "kernel" in stages:
        assert bundle is not None
        out.append(_fit(profile, "kernel", bundle.kernel))
    if "rootfs" in stages:
        assert bundle is not None
        out.append(_fit(profile, "rootfs", bundle.rootfs))
    if "env" in stages and wipe_env:
        out.append(_fit(profile, "env", b"\xff"))
    if "rootfs-data" in stages and wipe_rootfs_data:
        out.append(Segment("rootfs_data", rootfs_end, b"\xff" * (flash_size - rootfs_end)))
    return out


def restore_segments(image: bytes, flash_size: int) -> list[Segment]:
    """A whole-chip image. Both its length and its header are checked."""
    if len(image) != flash_size:
        raise GrainMediaError(
            f"the dump is {len(image)} bytes and the flash {flash_size}: "
            "only a whole-chip dump can be restored"
        )
    if image.startswith(HEADER_MAGIC):
        # Read through the vendor's U-Boot (every word swapped) or Linux (only
        # the headers), and the file does not say which: do not guess.
        raise GrainMediaError(
            "the dump is in the vendor's byte order, not in flash order. If it was read "
            "through the vendor's U-Boot, reverse the bytes of every 32-bit word first; "
            "a dump from the vendor's Linux is a mix and cannot be restored"
        )
    if not has_system_header(image):
        raise GrainMediaError(
            "the dump has no GM8126 system header at offset 0: not a GM8126 dump, "
            "or not in flash order"
        )
    return [Segment("flash", 0, image)]
