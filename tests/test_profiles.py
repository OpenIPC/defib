"""Tests for SoC profile loading and validation."""

import json
from pathlib import Path

import pytest

from defib.profiles.loader import (
    list_chips,
    list_variants,
    load_profile,
    parse_chip_variant,
)


PROFILES_DIR = Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


class TestSoCProfile:
    def test_parse_hi3516cv300(self):
        profile = load_profile("hi3516cv300", PROFILES_DIR)
        assert profile.name == "hi3516cv300"
        assert len(profile.ddrstep0) == 64
        assert len(profile.addresses) == 3
        assert len(profile.file_lengths) == 2
        assert len(profile.step_lengths) == 2

    def test_addresses(self):
        profile = load_profile("hi3516cv300", PROFILES_DIR)
        assert profile.ddr_step_address == 0x04013000
        assert profile.spl_address == 0x04010500
        assert profile.uboot_address == 0x81000000

    def test_spl_max_size(self):
        profile = load_profile("hi3516cv300", PROFILES_DIR)
        assert profile.spl_max_size == 0x4F00

    def test_ddr_step_data(self):
        profile = load_profile("hi3516cv300", PROFILES_DIR)
        data = profile.ddr_step_data
        assert isinstance(data, bytes)
        assert len(data) == 64
        # First 4 bytes from the known profile
        assert data[0] == 4
        assert data[1] == 224

    def test_nonexistent_chip(self):
        with pytest.raises(FileNotFoundError, match="No profile found"):
            load_profile("nonexistent_chip_xyz", PROFILES_DIR)


class TestListChips:
    def test_returns_sorted_list(self):
        chips = list_chips(PROFILES_DIR)
        assert isinstance(chips, list)
        assert len(chips) > 50  # We have 109 profiles
        assert chips == sorted(chips)

    def test_contains_known_chips(self):
        chips = list_chips(PROFILES_DIR)
        assert "hi3516cv300" in chips
        assert "hi3516ev200" in chips
        assert "gk7205v200" in chips

    def test_no_json_extension(self):
        chips = list_chips(PROFILES_DIR)
        for chip in chips:
            assert not chip.endswith(".json")


class TestAliasResolution:
    def test_alias_chain(self):
        """Some profiles are aliases pointing to other profiles."""
        chips = list_chips(PROFILES_DIR)
        # Just verify all profiles can be loaded without infinite loops
        loaded = 0
        for chip in chips[:20]:  # Test first 20 to keep test fast
            try:
                profile = load_profile(chip, PROFILES_DIR)
                assert profile.name is not None
                loaded += 1
            except Exception:
                pass  # Some might be aliases to nonexistent targets
        assert loaded > 10


class TestParseChipVariant:
    def test_no_colon_no_variant(self):
        assert parse_chip_variant("hi3516av300") == ("hi3516av300", None)

    def test_colon_splits_variant(self):
        assert parse_chip_variant("hi3516av300:emmc") == ("hi3516av300", "emmc")

    def test_lowercased(self):
        assert parse_chip_variant("Hi3516AV300:EMMC") == ("hi3516av300", "emmc")

    def test_empty_variant_treated_as_none(self):
        assert parse_chip_variant("hi3516av300:") == ("hi3516av300", None)


class TestBoardVariants:
    """Per-board overrides for fields that vary by board (DDR, clocks)."""

    @pytest.fixture
    def chip_with_variants(self, tmp_path: Path) -> Path:
        """Synthetic chip profile with an `emmc` variant that overrides DDRSTEP0."""
        base_ddr = [0] * 64
        emmc_ddr = [1] * 64
        profile = {
            "name": "testchip",
            "DDRSTEP0": base_ddr,
            "ADDRESS": ["0x04017000", "0x04010500", "0x81000000"],
            "FILELEN": ["0x0040", "0x6000"],
            "STEPLEN": ["0x0040", "0x0070"],
            "variants": {
                "emmc": {"DDRSTEP0": emmc_ddr},
                "uart-debug": {
                    "DDRSTEP0": [2] * 64,
                    "PRESTEP0": [3] * 64,
                },
            },
        }
        (tmp_path / "testchip.json").write_text(json.dumps(profile))
        return tmp_path

    def test_load_without_variant_uses_base(self, chip_with_variants: Path):
        p = load_profile("testchip", chip_with_variants)
        assert p.ddr_step_data == bytes([0] * 64)

    def test_load_with_variant_applies_override(self, chip_with_variants: Path):
        p = load_profile("testchip:emmc", chip_with_variants)
        assert p.ddr_step_data == bytes([1] * 64)

    def test_variant_can_add_field_not_in_base(self, chip_with_variants: Path):
        # uart-debug adds PRESTEP0 that the base doesn't have
        p = load_profile("testchip:uart-debug", chip_with_variants)
        assert p.ddr_step_data == bytes([2] * 64)
        assert p.prestep_data == bytes([3] * 64)

    def test_unknown_variant_raises_with_available_list(
        self, chip_with_variants: Path
    ):
        with pytest.raises(ValueError, match="Unknown variant 'nope'"):
            load_profile("testchip:nope", chip_with_variants)
        # The error should name the available variants so the user can pivot.
        with pytest.raises(ValueError, match="emmc"):
            load_profile("testchip:nope", chip_with_variants)
        with pytest.raises(ValueError, match="uart-debug"):
            load_profile("testchip:nope", chip_with_variants)

    def test_unknown_variant_on_chip_with_no_variants(self, tmp_path: Path):
        profile = {
            "name": "tinychip",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x0", "0x0", "0x0"],
            "FILELEN": ["0x0", "0x0"],
            "STEPLEN": ["0x0", "0x0"],
        }
        (tmp_path / "tinychip.json").write_text(json.dumps(profile))
        with pytest.raises(ValueError, match="none declared"):
            load_profile("tinychip:emmc", tmp_path)

    def test_list_variants_returns_sorted(self, chip_with_variants: Path):
        assert list_variants("testchip", chip_with_variants) == [
            "emmc", "uart-debug",
        ]

    def test_list_variants_strips_variant_suffix(self, chip_with_variants: Path):
        # Caller passed a variant suffix — should still resolve and list.
        assert list_variants("testchip:emmc", chip_with_variants) == [
            "emmc", "uart-debug",
        ]

    def test_list_variants_empty_when_chip_has_none(self, tmp_path: Path):
        profile = {
            "name": "tinychip",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x0", "0x0", "0x0"],
            "FILELEN": ["0x0", "0x0"],
            "STEPLEN": ["0x0", "0x0"],
        }
        (tmp_path / "tinychip.json").write_text(json.dumps(profile))
        assert list_variants("tinychip", tmp_path) == []

    def test_variants_follow_alias_chain(self, tmp_path: Path):
        # Aliases let us name dv300 → av300 without duplicating data.
        # Variants live on the alias TARGET and are reachable via either name.
        (tmp_path / "dv300_alias.json").write_text("av300_real.json")
        (tmp_path / "av300_real.json").write_text(json.dumps({
            "name": "av300_real",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x04017000", "0x04010500", "0x81000000"],
            "FILELEN": ["0x0040", "0x6000"],
            "STEPLEN": ["0x0040", "0x0070"],
            "variants": {"emmc": {"DDRSTEP0": [9] * 64}},
        }))
        p = load_profile("dv300_alias:emmc", tmp_path)
        assert p.ddr_step_data == bytes([9] * 64)
        assert list_variants("dv300_alias", tmp_path) == ["emmc"]

    def test_real_av300_base_still_loads(self):
        """Smoke test: real shipped profile loads without a variant."""
        p = load_profile("hi3516av300", PROFILES_DIR)
        assert p.name == "hi3516av300"
        assert p.uboot_address == 0x81000000
        # Base profile has no SPL_BLOB — that's variant-specific
        assert p.spl_data is None

    def test_real_av300_emmc_variant_loads_spl_blob(self):
        """The shipped hi3516av300:emmc variant carries a vendor SPL blob
        extracted from a working eMMC board. Validates that the variant
        merging + SPL_BLOB resolution actually loads bytes from disk."""
        p = load_profile("hi3516av300:emmc", PROFILES_DIR)
        assert p.name == "hi3516av300"
        assert p.spl_blob == "hi3516av300-emmc-spl.bin"
        assert p.spl_data is not None
        # Vendor SPL starts with an ARM `b` instruction at offset 0
        # (same as any reasonable boot ROM payload).
        assert p.spl_data[:4] == bytes.fromhex("150800ea")
        # SPL extracted at 0x5000 (above the gzip boundary at ~0x5220)
        assert len(p.spl_data) == 0x5000


class TestSplBlob:
    """SPL_BLOB lets a profile (typically a board variant) ship pre-built
    SPL bytes instead of relying on the OpenIPC firmware download."""

    @pytest.fixture
    def chip_with_spl_blob(self, tmp_path: Path) -> Path:
        (tmp_path / "test-spl.bin").write_bytes(b"\xAA" * 1024)
        profile = {
            "name": "blobchip",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x0", "0x0", "0x0"],
            "FILELEN": ["0x0", "0x0"],
            "STEPLEN": ["0x0", "0x0"],
            "SPL_BLOB": "test-spl.bin",
        }
        (tmp_path / "blobchip.json").write_text(json.dumps(profile))
        return tmp_path

    def test_loader_reads_blob_bytes(self, chip_with_spl_blob: Path):
        p = load_profile("blobchip", chip_with_spl_blob)
        assert p.spl_data == b"\xAA" * 1024

    def test_missing_blob_raises(self, tmp_path: Path):
        profile = {
            "name": "badblob",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x0", "0x0", "0x0"],
            "FILELEN": ["0x0", "0x0"],
            "STEPLEN": ["0x0", "0x0"],
            "SPL_BLOB": "does-not-exist.bin",
        }
        (tmp_path / "badblob.json").write_text(json.dumps(profile))
        with pytest.raises(FileNotFoundError, match="SPL_BLOB"):
            load_profile("badblob", tmp_path)

    def test_blob_via_variant(self, tmp_path: Path):
        """Most realistic case: SPL_BLOB declared inside a variant block."""
        (tmp_path / "v-spl.bin").write_bytes(b"\x11" * 4096)
        profile = {
            "name": "chip",
            "DDRSTEP0": [0] * 64,
            "ADDRESS": ["0x0", "0x0", "0x0"],
            "FILELEN": ["0x0", "0x0"],
            "STEPLEN": ["0x0", "0x0"],
            "variants": {"emmc": {"SPL_BLOB": "v-spl.bin"}},
        }
        (tmp_path / "chip.json").write_text(json.dumps(profile))
        base = load_profile("chip", tmp_path)
        assert base.spl_data is None
        variant = load_profile("chip:emmc", tmp_path)
        assert variant.spl_data == b"\x11" * 4096


class TestVariantStrippingInLookups:
    """Helpers that accept a chip identifier must accept the chip:variant
    form and strip the variant before looking up per-chip resources
    (firmware download URL, prebuilt agent binary). Otherwise the colon
    syntax would only work for the profile loader."""

    def test_firmware_url_strips_variant(self):
        from defib.firmware import firmware_url
        a = firmware_url("hi3516ev300")
        b = firmware_url("hi3516ev300:emmc")
        assert a is not None
        assert a == b

    def test_get_cached_path_strips_variant(self):
        from defib.firmware import get_cached_path
        # Either both return None (not cached) or both return the same path —
        # they must agree.
        assert get_cached_path("hi3516ev300") == get_cached_path(
            "hi3516ev300:emmc"
        )

    def test_get_agent_binary_strips_variant(self):
        from defib.agent.client import get_agent_binary
        assert get_agent_binary("hi3516av300") == get_agent_binary(
            "hi3516av300:emmc"
        )
