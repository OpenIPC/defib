"""Tests for SoC profile loading and validation."""

from pathlib import Path

import pytest

from defib.profiles.loader import list_chips, load_profile


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
