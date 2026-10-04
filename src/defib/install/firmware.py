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
    """

    kernel_name: str
    kernel: bytes
    rootfs_name: str
    rootfs: bytes


def _is_ubi_member(name: str) -> bool:
    """``rootfs.ubi.<board>`` (or bare ``rootfs.ubi``), never ``rootfs.ubifs.*``."""
    return name == "rootfs.ubi" or name.startswith("rootfs.ubi.")


def load_firmware_bundle(path: str | Path, *, ubi_only: bool = False) -> FirmwareBundle:
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

    if ubi_only:
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
