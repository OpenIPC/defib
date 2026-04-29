"""Tests for NAND flash install support and protocol robustness fixes."""

from defib.cli.app import (
    _NAND_LAYOUT,
    _NOR8M_LAYOUT,
    _NOR16M_LAYOUT,
    _nand_bootargs,
)


class TestNandLayout:
    """Verify NAND partition layout constants."""

    def test_nand_layout_partitions_exist(self):
        for key in ("boot", "env", "kernel", "rootfs"):
            assert key in _NAND_LAYOUT

    def test_nand_layout_offsets_contiguous(self):
        """Partitions must not overlap and boot+env+kernel must be contiguous."""
        b_off, b_sz = _NAND_LAYOUT["boot"]
        e_off, e_sz = _NAND_LAYOUT["env"]
        k_off, k_sz = _NAND_LAYOUT["kernel"]
        r_off, _r_sz = _NAND_LAYOUT["rootfs"]

        assert b_off == 0
        assert e_off == b_off + b_sz
        assert k_off == e_off + e_sz
        assert r_off == k_off + k_sz

    def test_nand_boot_env_sizes(self):
        """Boot and env are 1MB each (NAND erase-block aligned)."""
        assert _NAND_LAYOUT["boot"] == (0x000000, 0x100000)
        assert _NAND_LAYOUT["env"] == (0x100000, 0x100000)

    def test_nand_kernel_8mb(self):
        assert _NAND_LAYOUT["kernel"] == (0x200000, 0x800000)

    def test_nand_rootfs_starts_at_10mb(self):
        r_off, r_sz = _NAND_LAYOUT["rootfs"]
        assert r_off == 0xA00000  # 10MB
        assert r_sz > 0

    def test_nand_layout_larger_than_nor(self):
        """NAND partitions must be larger than NOR equivalents."""
        for key in ("boot", "env", "kernel", "rootfs"):
            _, nand_sz = _NAND_LAYOUT[key]
            _, nor_sz = _NOR8M_LAYOUT[key]
            assert nand_sz >= nor_sz, f"NAND {key} smaller than NOR 8M"

    def test_nor_layouts_unchanged(self):
        """Regression: NOR layouts must not be modified."""
        assert _NOR8M_LAYOUT["boot"] == (0x000000, 0x40000)
        assert _NOR8M_LAYOUT["kernel"] == (0x050000, 0x200000)
        assert _NOR16M_LAYOUT["boot"] == (0x000000, 0x40000)
        assert _NOR16M_LAYOUT["kernel"] == (0x050000, 0x300000)


class TestNandBootargs:
    """Verify defib sets bootargs that match the rootfs format we flash.

    Regression: U-Boot's compiled-in default bootargs is unreliable.  Recent
    OpenIPC builds default to ``rootfstype=squashfs`` even when the actual
    rootfs.ubi contains UBIFS, causing kernel panic ("Unable to mount root
    fs").  defib must set bootargs explicitly to match what it wrote.
    """

    def test_ubifs_bootargs_uses_ubi_root(self):
        """UBIFS rootfs: use ubi0:rootfs (kernel attaches the UBI volume)."""
        args = _nand_bootargs(rootfs_is_ubi=True)
        assert "root=ubi0:rootfs" in args
        assert "rootfstype=ubifs" in args
        # Must not contain squashfs-specific bits
        assert "squashfs" not in args
        assert "ubiblock" not in args
        assert "ubi.block" not in args

    def test_squashfs_bootargs_uses_ubiblock(self):
        """Non-UBI rootfs: use ubiblock0_0 with squashfs filesystem type."""
        args = _nand_bootargs(rootfs_is_ubi=False)
        assert "root=/dev/ubiblock0_0" in args
        assert "rootfstype=squashfs" in args
        assert "ubi.block=0,0" in args
        # Must not contain UBIFS-specific root
        assert "ubi0:rootfs" not in args

    def test_bootargs_matches_mtdparts_layout(self):
        """ubi.mtd index must match the partition layout (boot,env,kernel,ubi
        → mtd3 = ubi).  Otherwise UBI attaches the wrong partition."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "ubi.mtd=3,2048" in args, args
            assert (
                "mtdparts=hinand:1024k(boot),1024k(env),"
                "8192k(kernel),-(ubi)"
            ) in args, args

    def test_bootargs_includes_console_and_panic(self):
        """Standard fields needed for a usable rescue boot — serial console
        for debugging and panic-reboot so a broken boot doesn't hang."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "console=ttyAMA0,115200" in args
            assert "panic=20" in args

    def test_bootargs_is_single_line(self):
        """No newlines or null bytes — saveenv must store it as a single
        bootargs= line."""
        for is_ubi in (True, False):
            args = _nand_bootargs(rootfs_is_ubi=is_ubi)
            assert "\n" not in args
            assert "\x00" not in args
            assert "\r" not in args
