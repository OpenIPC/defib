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

from defib.profiles.loader import load_profile, recovery_mode
from defib.profiles.schema import FlashPartition, SoCProfile

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
        """Read off a real Luckfox Pico Max, not the docs.

        Its U-Boot env and /proc/mtd both give::

            spi-nand0:256K(env),256K@256K(idblock),512K(uboot),4M(boot),
                      30M(oem),10M(userdata),210M(rootfs)

        Note rootfs is 210M. Luckfox's published layout says 80M, which is
        what this profile shipped with until hardware contradicted it.
        """
        partitions = load_profile("rv1106", PROFILES_DIR).partitions
        K, M = 1024, 1024 * 1024
        expected = {
            "env": (0, 256 * K),
            "idblock": (256 * K, 256 * K),
            "uboot": (512 * K, 512 * K),
            "boot": (1 * M, 4 * M),
            "oem": (5 * M, 30 * M),
            "userdata": (35 * M, 10 * M),
            "rootfs": (45 * M, 210 * M),
        }
        actual = {
            name: (p.lba * 512, p.size_bytes) for name, p in partitions.items()
        }
        assert actual == expected

    def test_partitions_tile_without_gaps_or_overlap(self):
        """A gap or an overlap here would show up as one image silently
        landing inside another."""
        partitions = load_profile("rv1106", PROFILES_DIR).partitions
        extents = sorted(partitions.values(), key=lambda p: p.lba)
        for lower, upper in zip(extents, extents[1:]):
            assert lower.end_lba == upper.lba

    def test_idblock_stays_at_its_fixed_offset(self):
        """The boot ROM looks for the IDB at 0x40000; moving it bricks the
        board in a way no button recovers."""
        partitions = load_profile("rv1106", PROFILES_DIR).partitions
        assert partitions["idblock"].lba * 512 == 0x40000

    def test_profile_json_is_minimal(self):
        """A USB profile carrying UART bytecode would mean someone copied the
        wrong template."""
        data = json.loads((PROFILES_DIR / "rv1106.json").read_text(encoding="utf-8"))
        assert not {"DDRSTEP0", "PRESTEP0", "ADDRESS", "FILELEN"} & set(data)


class TestFlashPartition:
    def test_end_lba(self):
        assert FlashPartition(lba=100, sectors=50).end_lba == 150

    def test_size_bytes(self):
        assert FlashPartition(lba=0, sectors=8192).size_bytes == 4 * 1024 * 1024


class TestRecoveryModeErrorHandling:
    """recovery_mode() must default to UART only for a chip with no profile.

    Swallowing every ValueError would route a mistyped USB chip into the
    serial workflow and then report the wrong problem entirely — a failure to
    open a serial port, rather than "that variant does not exist".
    """

    def test_missing_profile_defaults_to_uart(self):
        assert recovery_mode("no-such-chip-at-all", PROFILES_DIR) == "uart"

    def test_unknown_variant_propagates(self):
        with pytest.raises(ValueError, match="Unknown variant"):
            recovery_mode("rv1106:typo", PROFILES_DIR)

    def test_unknown_variant_on_uart_chip_also_propagates(self):
        with pytest.raises(ValueError, match="Unknown variant"):
            recovery_mode("hi3516cv300:nope", PROFILES_DIR)

    def test_declared_variant_still_resolves(self):
        from defib.profiles.loader import list_variants

        for chip in ("hi3516cv300", "rv1106"):
            for variant in list_variants(chip, PROFILES_DIR):
                assert recovery_mode(f"{chip}:{variant}", PROFILES_DIR) in ("uart", "usb")
