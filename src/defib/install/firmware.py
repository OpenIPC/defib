"""OpenIPC firmware archive loading and integrity checks."""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from pathlib import Path

from defib.uboot_tftp import uboot_tftp_commands as uboot_tftp_commands


@dataclass(frozen=True)
class FirmwareBundle:
    """Kernel and rootfs payloads extracted from one OpenIPC firmware archive.

    For a UBI-only NAND package the kernel lives inside the rootfs image
    (``/boot/fitImage`` in the UBIFS volume): ``kernel_name`` is empty and
    ``kernel`` is ``b""``.

    For a split NOR package (``openipc.<board>-nor-<variant>-split.tgz``) the
    one image is ``firmware.bin.<board>``: it is carried as the rootfs, the
    kernel is empty, and ``split`` is set.
    """

    kernel_name: str
    kernel: bytes
    rootfs_name: str
    rootfs: bytes
    split: bool = False


ERASE_BLOCK = 0x10000
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
SQUASHFS_MAGIC = b"hsqs"


def split_image_end(image: bytes) -> int:
    """Where rootfs_data starts, relative to the image, for a split firmware.bin.

    The FIT's ``totalsize`` (header bytes 4..7, big-endian) rounded up to a
    64 KiB block is where the squashfs starts; its ``bytes_used`` (superblock
    offset 40, little-endian) rounded up the same way is where it ends. The
    kernel's mtdsplit, U-Boot and sysupgrade all draw the same lines.
    Raises ValueError for anything else.
    """
    if image[:4] != FDT_MAGIC:
        raise ValueError("firmware.bin does not start with a FIT image")
    fit = int.from_bytes(image[4:8], "big")
    squash = (fit + ERASE_BLOCK - 1) // ERASE_BLOCK * ERASE_BLOCK
    if not fit or squash + 48 > len(image) or image[squash:squash + 4] != SQUASHFS_MAGIC:
        raise ValueError("firmware.bin has no squashfs at the 64 KiB block after its FIT")
    used = int.from_bytes(image[squash + 40:squash + 48], "little")
    end = squash + (used + ERASE_BLOCK - 1) // ERASE_BLOCK * ERASE_BLOCK
    if not used or len(image) < squash + used:
        raise ValueError(
            "firmware.bin ends inside its squashfs: the image is truncated"
        )
    if end < len(image):
        raise ValueError(
            "firmware.bin runs past the end of its squashfs: not an image as the "
            "OpenIPC build packs it"
        )
    return end


def _is_ubi_member(name: str) -> bool:
    """``rootfs.ubi.<board>`` (or bare ``rootfs.ubi``), never ``rootfs.ubifs.*``."""
    return name == "rootfs.ubi" or name.startswith("rootfs.ubi.")


def load_firmware_bundle(
    path: str | Path, *, ubi_only: bool = False, boards: set[str] | None = None
) -> FirmwareBundle:
    """Read kernel/rootfs and verify any matching md5sum entries in one pass.

    ``ubi_only`` selects the UBI-only NAND package
    (``openipc.<board>-nand-<variant>.tgz``): the payload is the single
    ``rootfs.ubi.<board>`` image, which already contains the kernel, and no
    ``uImage`` is expected.
    """
    kernel_name = ""
    kernel: bytes | None = None
    rootfs_name = ""
    rootfs: bytes | None = None
    ubi_members: list[str] = []
    split_name = ""
    split_image: bytes | None = None
    expected_md5: dict[str, str] = {}

    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            name = member.name
            if name.endswith(".md5sum"):
                line = stream.read().decode().strip()
                if line:
                    expected_md5[name.removesuffix(".md5sum")] = line.split()[0]
            elif name.rsplit("/", 1)[-1].startswith("firmware.bin"):
                split_name = name
                split_image = stream.read()
            elif ubi_only:
                if _is_ubi_member(name):
                    ubi_members.append(name)
                    rootfs_name = name
                    rootfs = stream.read()
            elif name.startswith("uImage"):
                kernel_name = name
                kernel = stream.read()
            elif name.startswith("rootfs.squashfs") or _is_ubi_member(name):
                rootfs_name = name
                rootfs = stream.read()

    if split_image is not None and not ubi_only:
        if kernel or rootfs:
            raise ValueError(
                "tarball holds both firmware.bin and a separate kernel/rootfs; "
                "expected one OpenIPC package or the other"
            )
        expected = expected_md5.get(split_name)
        if expected is not None and hashlib.md5(split_image).hexdigest() != expected:
            raise ValueError(f"MD5 mismatch for {split_name}")
        split_image_end(split_image)
        board = split_name.rsplit("/", 1)[-1].removeprefix("firmware.bin").lstrip(".")
        if boards is not None and board not in boards:
            raise ValueError(
                f"{split_name} is built for {board or 'an unnamed board'}, not "
                + " or ".join(sorted(boards))
                + "; refusing to write another board's firmware"
            )
        return FirmwareBundle(
            kernel_name="",
            kernel=b"",
            rootfs_name=split_name,
            rootfs=split_image,
            split=True,
        )

    if ubi_only:
        if not rootfs and split_image is not None:
            raise ValueError(
                f"{split_name} is a split NOR package (openipc.<board>-nor-<variant>"
                "-split.tgz); it cannot be installed on NAND"
            )
        if not rootfs:
            raise ValueError(
                "tarball has no rootfs.ubi.<board> image; this NAND layout needs "
                "the OpenIPC NAND package openipc.<board>-nand-<variant>.tgz"
            )
        if len(ubi_members) > 1:
            raise ValueError(
                "tarball has more than one rootfs.ubi image: " + ", ".join(ubi_members)
            )
        from defib.ubi import is_ubi_image

        if not is_ubi_image(rootfs):
            raise ValueError(f"{rootfs_name} is not a UBI image (no UBI# header)")
        # The image carries its kernel, so one built for another board would
        # put that board's kernel and rootfs on this camera's NAND.
        board = rootfs_name.rsplit("/", 1)[-1].removeprefix("rootfs.ubi.")
        if boards is not None and board not in boards:
            raise ValueError(
                f"{rootfs_name} is built for {board}, not "
                + " or ".join(sorted(boards))
                + "; refusing to write another board's NAND image"
            )
        kernel = b""
    elif not kernel or not rootfs:
        raise ValueError("tarball missing uImage or rootfs (squashfs/ubi)")

    assert kernel is not None and rootfs is not None
    for name, data in ((kernel_name, kernel), (rootfs_name, rootfs)):
        if not name:
            continue
        expected = expected_md5.get(name)
        if expected is not None and hashlib.md5(data).hexdigest() != expected:
            raise ValueError(f"MD5 mismatch for {name}")

    return FirmwareBundle(
        kernel_name=kernel_name,
        kernel=kernel,
        rootfs_name=rootfs_name,
        rootfs=rootfs,
    )
