"""Tests for firmware auto-download module."""

from pathlib import Path

import pytest

from defib.firmware import (
    AVAILABLE_FIRMWARE,
    CLASSIC_UBOOT_VARIANTS,
    PER_FLASH_TYPE_UBOOT,
    CV6XX_BOOT_VARIANTS,
    asset_name,
    download_firmware,
    firmware_url,
    get_cache_dir,
    get_cached_path,
    has_firmware,
    pad_to_size,
)


class TestFirmwareUrl:
    def test_known_chip(self):
        url = firmware_url("hi3516ev300")
        assert url is not None
        assert "hi3516ev300" in url
        assert url.endswith(".bin")

    def test_unknown_chip(self):
        assert firmware_url("nonexistent_chip_xyz") is None

    def test_aliased_chip(self):
        url = firmware_url("hi3518ev201")
        assert url is not None
        assert "hi3518ev200" in url

    def test_url_format(self):
        url = firmware_url("gk7205v200")
        assert url == "https://github.com/OpenIPC/firmware/releases/download/latest/u-boot-gk7205v200-universal.bin"


class TestHasFirmware:
    def test_available_chips(self):
        for chip in ["hi3516ev300", "gk7205v200", "hi3518ev200"]:
            assert has_firmware(chip), f"{chip} should be available"

    def test_unavailable_chips(self):
        for chip in ["hi3716mv300", "hi3751v500", "nonexistent"]:
            assert not has_firmware(chip), f"{chip} should not be available"

    def test_aliased_chip(self):
        assert has_firmware("hi3518ev201")
        assert has_firmware("gk7201v200")


class TestAvailableFirmware:
    def test_at_least_20_chips(self):
        assert len(AVAILABLE_FIRMWARE) >= 20

    def test_common_chips_included(self):
        for chip in ["gk7205v200", "hi3518ev200"]:
            assert chip in AVAILABLE_FIRMWARE

    def test_per_flash_type_socs_are_not_universal(self):
        assert not AVAILABLE_FIRMWARE & PER_FLASH_TYPE_UBOOT


class TestCacheDir:
    def test_returns_path(self):
        d = get_cache_dir()
        assert isinstance(d, Path)
        assert d.exists()
        assert "defib" in str(d)

    def test_cached_path_missing(self):
        assert get_cached_path("nonexistent_chip_xyz") is None


class TestPadToSize:
    def test_pads_short_input_with_ff(self):
        # Issue #73: producer dropped 1 MiB padding; consumer pads now.
        # 199276-byte raw u-boot → 1 MiB partition.
        raw = b"\xde\xad\xbe\xef" * 49819  # 199276 bytes
        padded = pad_to_size(raw, 0x100000)
        assert len(padded) == 0x100000
        assert padded[: len(raw)] == raw
        assert padded[len(raw):] == b"\xff" * (0x100000 - len(raw))

    def test_exact_size_unchanged(self):
        data = b"\x00\x11\x22\x33"
        assert pad_to_size(data, 4) == data

    def test_oversize_raises(self):
        with pytest.raises(ValueError, match="larger than target"):
            pad_to_size(b"\x00" * 10, 4)

    def test_custom_fill_byte(self):
        assert pad_to_size(b"x", 4, fill=0x00) == b"x\x00\x00\x00"

    def test_empty_input(self):
        assert pad_to_size(b"", 3) == b"\xff\xff\xff"


class TestCV6xxBootImages:
    """CV6xx SoCs publish boot-{chip}[-{variant}]-nor.bin composite images.

    #112 registered hi3516cv608/hi3516cv610 in AVAILABLE_FIRMWARE, which made
    firmware_url() build a u-boot-{chip}-universal.bin URL that has never
    existed for any CV6xx chip, so every auto-download 404'd.
    """

    def test_cv608_resolves_to_composite_boot_image(self):
        assert asset_name("hi3516cv608") == "boot-hi3516cv608-nor.bin"

    def test_cv6xx_never_uses_universal_naming(self):
        for chip in CV6XX_BOOT_VARIANTS:
            assert chip not in AVAILABLE_FIRMWARE
            for name in (chip, f"{chip}:00g", f"{chip}:dmeb"):
                resolved = asset_name(name)
                if resolved is not None:
                    assert "universal" not in resolved
                    assert resolved.startswith("boot-")

    def test_variant_selects_the_matching_image(self):
        assert asset_name("hi3516cv610:00g") == "boot-hi3516cv610-00g-nor.bin"
        assert asset_name("hi3516cv610:20s") == "boot-hi3516cv610-20s-nor.bin"
        assert asset_name("hi3519dv500:dmebpro") == "boot-hi3519dv500-dmebpro-nor.bin"

    def test_ambiguous_chip_is_not_resolved(self):
        # Five hi3516cv610 images carry different DDR settings; guessing one
        # would flash the wrong DDR config, so refuse instead.
        assert asset_name("hi3516cv610") is None
        assert firmware_url("hi3516cv610") is None
        assert asset_name("hi3519dv500") is None

    def test_unknown_variant_is_not_resolved(self):
        assert asset_name("hi3516cv610:nosuchboard") is None

    def test_single_image_chip_ignores_variant_suffix(self):
        # Only one cv608 image exists, so a suffix carries no meaning.
        assert asset_name("hi3516cv608:anything") == "boot-hi3516cv608-nor.bin"

    def test_chip_without_published_image_stays_unavailable(self):
        for chip in ["hi3516cv613", "hi3516dv500"]:
            assert firmware_url(chip) is None

    def test_ambiguous_variant_error_lists_the_options(self, tmp_path, monkeypatch):
        # Isolate the cache: a developer who hand-seeded a cv610 blob per #112
        # would otherwise get that path back instead of the error.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(ValueError, match="explicit board variant") as exc:
            download_firmware("hi3516cv610")
        for variant in CV6XX_BOOT_VARIANTS["hi3516cv610"]:
            assert f"hi3516cv610:{variant}" in str(exc.value)


class TestLegacyCacheSeeding:
    """#112 told CV6xx users to hand-seed the cache under the universal name.
    Those files must keep working now that the real asset name differs."""

    def test_hand_seeded_legacy_blob_is_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        seeded = get_cache_dir() / "u-boot-hi3516cv610-universal.bin"
        seeded.write_bytes(b"\x00" * 4096)

        assert get_cached_path("hi3516cv610") == seeded
        # ...and it satisfies the install gate even though no URL resolves.
        assert firmware_url("hi3516cv610") is None
        assert has_firmware("hi3516cv610")

    def test_no_seed_means_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        assert get_cached_path("hi3516cv610") is None
        assert not has_firmware("hi3516cv610")

    def test_empty_cache_file_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        (get_cache_dir() / "boot-hi3516cv608-nor.bin").write_bytes(b"")
        assert get_cached_path("hi3516cv608") is None


class TestClassicChipsUnchanged:
    def test_universal_naming_preserved(self):
        assert asset_name("hi3516cv300") == "u-boot-hi3516cv300-universal.bin"

    def test_flash_type_does_not_rename_universal_images(self):
        assert asset_name("hi3516cv300", "nand") == "u-boot-hi3516cv300-universal.bin"

    def test_variant_still_ignored_for_classic_socs(self):
        assert asset_name("hi3516ev300:emmc") == asset_name("hi3516ev300")

    def test_alias_still_applied(self):
        assert asset_name("hi3518ev201") == "u-boot-hi3518ev200-universal.bin"

    def test_chips_present_in_release_are_offered(self):
        # Shipped u-boot-*-universal.bin assets that the set used to omit.
        for chip in ["hi3520dv200", "hi3536cv100", "hi3536dv100"]:
            assert has_firmware(chip)


class TestClassicBoardVariants:
    def test_ds_i203_resolves_to_dedicated_release_asset(self):
        selector = "hi3518ev100:hiwatch-ds-i203"
        assert asset_name(selector) == "u-boot-hi3518ev100-ddr3-256m-universal.bin"
        assert firmware_url(selector) == (
            "https://github.com/OpenIPC/firmware/releases/download/latest/"
            "u-boot-hi3518ev100-ddr3-256m-universal.bin"
        )

    def test_generic_hi3518ev100_stays_universal(self):
        assert asset_name("hi3518ev100") == "u-boot-hi3518ev100-universal.bin"

    def test_unknown_registered_classic_variant_is_rejected(self):
        assert "hi3518ev100" in CLASSIC_UBOOT_VARIANTS
        assert asset_name("hi3518ev100:not-a-board") is None

    def test_board_variant_never_falls_back_to_universal_cache(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        cache = get_cache_dir()
        (cache / "u-boot-hi3518ev100-universal.bin").write_bytes(b"U" * 4096)
        assert get_cached_path("hi3518ev100:hiwatch-ds-i203") is None

        dedicated = cache / "u-boot-hi3518ev100-ddr3-256m-universal.bin"
        dedicated.write_bytes(b"D" * 182580)
        assert get_cached_path("hi3518ev100:hiwatch-ds-i203") == dedicated


class TestV500Donor:
    def test_downloads_the_nor_build_once(self, monkeypatch, tmp_path):
        import io

        from defib import firmware

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        urls: list[str] = []

        class Resp(io.BytesIO):
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()

        def fake_urlopen(req, timeout=None):
            urls.append(req.full_url)
            return Resp(b"\x00" * 4096)

        monkeypatch.setattr(firmware.urllib.request, "urlopen", fake_urlopen)
        first = firmware.download_v500_donor("gk7205v510")
        again = firmware.download_v500_donor("gk7205v510")
        assert first == again == get_cache_dir() / "u-boot-gk7205v510-nor.bin"
        assert urls == [
            "https://github.com/OpenIPC/u-boot-xmedia/releases/download/latest/"
            "u-boot-gk7205v510-nor.bin"
        ]


class TestPerFlashTypeUBoot:
    """u-boot-xmedia SoCs publish u-boot-<soc>-{nor,nand}.bin, not -universal."""

    SOCS = (
        "hi3516ev200", "hi3516ev300", "hi3518ev300", "hi3516dv200",
        "gk7205v500", "gk7205v510", "gk7205v530",
    )

    def test_set_matches_the_published_socs(self):
        assert set(self.SOCS) == PER_FLASH_TYPE_UBOOT

    @pytest.mark.parametrize("soc", SOCS)
    def test_name_per_flash_type(self, soc):
        assert asset_name(soc, "nor") == f"u-boot-{soc}-nor.bin"
        assert asset_name(soc, "nand") == f"u-boot-{soc}-nand.bin"
        assert firmware_url(soc, "nand") == (
            "https://github.com/OpenIPC/firmware/releases/download/latest/"
            f"u-boot-{soc}-nand.bin"
        )

    @pytest.mark.parametrize("soc", SOCS)
    def test_nor_is_the_default(self, soc):
        assert asset_name(soc) == f"u-boot-{soc}-nor.bin"
        assert has_firmware(soc)

    def test_flash_type_is_case_insensitive_and_validated(self):
        assert asset_name("hi3516ev300", "NAND") == "u-boot-hi3516ev300-nand.bin"
        with pytest.raises(ValueError):
            asset_name("hi3516ev300", "emmc")

    def test_variant_suffix_is_ignored(self):
        assert asset_name("hi3516ev300:emmc", "nand") == "u-boot-hi3516ev300-nand.bin"

    def test_legacy_universal_cache_is_not_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        cache = get_cache_dir()
        (cache / "u-boot-hi3516ev300-universal.bin").write_bytes(b"U" * 4096)
        assert get_cached_path("hi3516ev300") is None
        assert get_cached_path("hi3516ev300", "nand") is None

        nand = cache / "u-boot-hi3516ev300-nand.bin"
        nand.write_bytes(b"N" * 4096)
        assert get_cached_path("hi3516ev300", "nand") == nand
        assert get_cached_path("hi3516ev300", "nor") is None

    def test_downloads_the_flash_type_build_never_universal(self, tmp_path, monkeypatch):
        from defib import firmware

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        urls: list[str] = []

        def fake_download(url, dest, on_progress=None):
            urls.append(url)
            dest.write_bytes(b"X" * 4096)
            return dest

        monkeypatch.setattr(firmware, "_download", fake_download)
        path = download_firmware("hi3516ev300", flash_type="nand")
        assert path.name == "u-boot-hi3516ev300-nand.bin"
        path = download_firmware("hi3516ev300")
        assert path.name == "u-boot-hi3516ev300-nor.bin"
        assert all("universal" not in url for url in urls)
        assert [u.rsplit("/", 1)[1] for u in urls] == [
            "u-boot-hi3516ev300-nand.bin",
            "u-boot-hi3516ev300-nor.bin",
        ]

    def test_cached_universal_is_an_offline_nor_fallback_only(self, tmp_path, monkeypatch):
        from defib import firmware

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        legacy = get_cache_dir() / "u-boot-hi3516ev300-universal.bin"
        legacy.write_bytes(b"U" * 4096)

        def offline(url, dest, on_progress=None):
            raise ConnectionError("offline")

        monkeypatch.setattr(firmware, "_download", offline)
        assert download_firmware("hi3516ev300") == legacy
        with pytest.raises(ConnectionError):
            download_firmware("hi3516ev300", flash_type="nand")

    def test_gk7205v5xx_shares_the_v500_donor_cache_entry(self, tmp_path, monkeypatch):
        # download_v500_donor() caches u-boot-xmedia's NOR build under the
        # same name the OpenIPC/firmware release uses, so either source
        # satisfies the other.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.platform", "linux")
        donor = get_cache_dir() / "u-boot-gk7205v510-nor.bin"
        donor.write_bytes(b"D" * 4096)
        assert get_cached_path("gk7205v510") == donor
        assert get_cached_path("gk7205v510", "nand") is None
