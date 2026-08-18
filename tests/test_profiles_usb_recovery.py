"""Tests for the USB-recovery discriminator on SoC profiles.

Chips whose boot ROM has no UART download path (Rockchip) declare
``"RECOVERY": "usb"`` and carry none of the DDR/SPL bytecode. These tests pin
down that the two families stay distinguishable and that neither can silently
be missing what it needs.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from defib.cli.app import _map_usb_images
from defib.profiles.loader import load_profile, recovery_mode
from defib.profiles.schema import SoCProfile

PROFILES_DIR = Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


class TestRecoveryDiscriminator:
    def test_uart_is_the_default(self):
        """Every pre-existing profile predates the field and must stay UART."""
        assert load_profile("hi3516cv300", PROFILES_DIR).recovery == "uart"

    def test_rv1106_is_usb(self):
        assert load_profile("rv1106", PROFILES_DIR).recovery == "usb"

    def test_recovery_mode_helper(self):
        assert recovery_mode("hi3516cv300", PROFILES_DIR) == "uart"
        assert recovery_mode("rv1106", PROFILES_DIR) == "usb"

    def test_unknown_chip_defaults_to_uart(self):
        """V500/CV6xx chips have no JSON profile, so absence must not be
        mistaken for USB recovery."""
        assert recovery_mode("hi3516dv500", PROFILES_DIR) == "uart"
        assert recovery_mode("no-such-chip-at-all", PROFILES_DIR) == "uart"


class TestValidation:
    def test_uart_profile_missing_bytecode_is_rejected(self):
        with pytest.raises(ValidationError, match="DDRSTEP0"):
            SoCProfile.model_validate({"name": "bogus"})

    def test_uart_error_names_every_missing_field(self):
        with pytest.raises(ValidationError) as excinfo:
            SoCProfile.model_validate({"name": "bogus", "DDRSTEP0": [1]})
        message = str(excinfo.value)
        for alias in ("ADDRESS", "FILELEN", "STEPLEN"):
            assert alias in message

    def test_usb_profile_needs_no_bytecode(self):
        profile = SoCProfile.model_validate({"name": "x", "RECOVERY": "usb"})
        assert profile.recovery == "usb"
        assert profile.ddrstep0 is None

    def test_uart_properties_raise_on_usb_profile(self):
        profile = SoCProfile.model_validate({"name": "x", "RECOVERY": "usb"})
        for attr in (
            "ddr_step_address", "spl_address", "uboot_address",
            "spl_max_size", "ddr_step_data",
        ):
            with pytest.raises(ValueError, match="recovers over usb"):
                getattr(profile, attr)

    def test_unknown_recovery_value_rejected(self):
        with pytest.raises(ValidationError):
            SoCProfile.model_validate({"name": "x", "RECOVERY": "jtag"})


class TestRv1106Profile:
    def test_declares_its_loader_blobs(self):
        profile = load_profile("rv1106", PROFILES_DIR)
        assert profile.loader_ddr == "rv1106_ddr_924MHz_v1.15.bin"
        assert profile.loader_usbplug == "rv1106_usbplug_v1.09.bin"

    def test_partition_lbas_match_the_vendor_byte_layout(self):
        """Luckfox SPI NAND: 256K(env) 256K@256K(idblock) 512K(uboot) 4M(boot)
        30M(oem) 10M(userdata) 80M(rootfs), converted to 512-byte sectors."""
        partitions = load_profile("rv1106", PROFILES_DIR).partitions
        expected_bytes = {
            "env": 0,
            "idblock": 256 * 1024,
            "uboot": 512 * 1024,
            "boot": 1024 * 1024,
            "oem": 5 * 1024 * 1024,
            "userdata": 35 * 1024 * 1024,
            "rootfs": 45 * 1024 * 1024,
        }
        assert partitions == {k: v // 512 for k, v in expected_bytes.items()}

    def test_idblock_stays_at_its_fixed_offset(self):
        """The boot ROM looks for the IDB at 0x40000; moving it bricks the
        board in a way no button recovers."""
        assert load_profile("rv1106", PROFILES_DIR).partitions["idblock"] * 512 == 0x40000

    def test_profile_json_is_minimal(self):
        """A USB profile carrying UART bytecode would mean someone copied the
        wrong template."""
        data = json.loads((PROFILES_DIR / "rv1106.json").read_text())
        assert not {"DDRSTEP0", "PRESTEP0", "ADDRESS", "FILELEN"} & set(data)


class TestMapUsbImages:
    PARTS = {"boot": 2048, "rootfs": 92160, "uboot": 1024, "idblock": 512}

    def test_maps_nor_style_tarball(self):
        out = _map_usb_images(
            ["zboot.img.rv1106", "rootfs.squashfs.rv1106"], self.PARTS
        )
        assert out == [
            ("zboot.img.rv1106", "boot", 2048),
            ("rootfs.squashfs.rv1106", "rootfs", 92160),
        ]

    def test_ubi_image_refused_with_the_reason(self):
        """Not a mapping gap — a UBI bundles kernel and rootfs volumes, so no
        single partition is the right answer."""
        import typer

        with pytest.raises(typer.BadParameter, match="kernel and rootfs"):
            _map_usb_images(["rootfs.ubi.rv1106"], self.PARTS)

    def test_undeclared_partition_is_an_error_not_a_silent_skip(self):
        import typer

        with pytest.raises(typer.BadParameter, match="does not declare"):
            _map_usb_images(["zboot.img.rv1106"], {"rootfs": 1})

    def test_unrecognised_files_are_ignored(self):
        assert _map_usb_images(["README", "notes.txt"], self.PARTS) == []

    def test_checksums_are_not_mistaken_for_images(self):
        out = _map_usb_images(["zboot.img.rv1106.md5sum"], self.PARTS)
        assert out == []
