"""OpenIPC firmware archive loading and integrity checks."""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from pathlib import Path

from defib.uboot_tftp import uboot_tftp_commands as uboot_tftp_commands


@dataclass(frozen=True)
class FirmwareBundle:
    """Kernel and rootfs payloads extracted from one OpenIPC firmware archive."""

    kernel_name: str
    kernel: bytes
    rootfs_name: str
    rootfs: bytes


def load_firmware_bundle(path: str | Path) -> FirmwareBundle:
    """Read kernel/rootfs and verify any matching md5sum entries in one pass."""
    kernel_name = ""
    kernel: bytes | None = None
    rootfs_name = ""
    rootfs: bytes | None = None
    expected_md5: dict[str, str] = {}

    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            if member.name.endswith(".md5sum"):
                line = stream.read().decode().strip()
                if line:
                    expected_md5[member.name.removesuffix(".md5sum")] = line.split()[0]
            elif member.name.startswith("uImage"):
                kernel_name = member.name
                kernel = stream.read()
            elif member.name.startswith(("rootfs.squashfs", "rootfs.ubi")):
                rootfs_name = member.name
                rootfs = stream.read()

    if not kernel or not rootfs:
        raise ValueError("tarball missing uImage or rootfs (squashfs/ubi)")

    for name, data in ((kernel_name, kernel), (rootfs_name, rootfs)):
        expected = expected_md5.get(name)
        if expected is not None and hashlib.md5(data).hexdigest() != expected:
            raise ValueError(f"MD5 mismatch for {name}")

    return FirmwareBundle(
        kernel_name=kernel_name,
        kernel=kernel,
        rootfs_name=rootfs_name,
        rootfs=rootfs,
    )
