"""Tests for NAND flash install support and protocol robustness fixes."""

from defib.cli.app import _NAND_LAYOUT, _NOR8M_LAYOUT, _NOR16M_LAYOUT


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
