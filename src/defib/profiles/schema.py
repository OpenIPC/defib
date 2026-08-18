"""Pydantic models for SoC profile data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator


class SoCProfile(BaseModel):
    """A SoC configuration profile.

    Field names match the JSON profile format used by the original burn tool,
    which was HiSilicon-only. ``RECOVERY`` widens that: chips whose boot ROM
    has no UART download path at all — Rockchip's, which is USB-only — declare
    ``"RECOVERY": "usb"`` and supply none of the DDR/SPL bytecode below.
    """

    name: str = Field(description="Internal chip name")
    recovery: Literal["uart", "usb"] = Field(
        default="uart", alias="RECOVERY",
        description=(
            "How a dead board is reached. 'uart' drives the boot ROM over "
            "serial with the bytecode in this profile. 'usb' means the boot "
            "ROM only answers on USB, so the fields below do not apply and "
            "the loader blobs are named by LOADER_* instead."
        ),
    )
    prestep0: list[int] | None = Field(
        default=None, alias="PRESTEP0",
        description="Pre-DDR init bytecode (sent before DDRSTEP0)",
    )
    prestep1: list[int] | None = Field(
        default=None, alias="PRESTEP1",
        description="DDR training verification bytecode (sent after DDRSTEP0)",
    )
    ddrstep0: list[int] | None = Field(
        default=None, alias="DDRSTEP0", description="DDR initialization bytecode"
    )
    addresses: list[str] | None = Field(
        default=None, alias="ADDRESS",
        description="Load addresses: [ddr_step, spl, uboot]",
    )
    file_lengths: list[str] | None = Field(
        default=None, alias="FILELEN",
        description="Size limits: [ddr_step_max, spl_max]",
    )
    step_lengths: list[str] | None = Field(
        default=None, alias="STEPLEN",
        description="Step frame sizes: [ddr_step, spl]",
    )
    sram_limit: str | None = Field(
        default=None, alias="SRAMLIMIT",
        description=(
            "Hex string. Hard ceiling on SPL upload size (chip SRAM window "
            "from spl_address to SRAM end). When set, _detect_spl_size will "
            "not return a value larger than this, even if it auto-detects a "
            "compressed-payload boundary further into the firmware. Required "
            "for single-blob mini-boot binaries whose LZMA payload sits past "
            "the chip's actual SRAM ceiling."
        ),
    )
    spl_blob: str | None = Field(
        default=None, alias="SPL_BLOB",
        description=(
            "Optional filename (resolved relative to the profile JSON's "
            "directory) of a pre-built SPL binary to upload as the SPL stage "
            "instead of slicing it from the downloaded U-Boot. Used by board "
            "variants where the OpenIPC SPL doesn't bring DDR up correctly — "
            "e.g. eMMC-equipped hi3516av300 boards. The loader reads the file "
            "and stores its bytes on `_spl_data`; callers access them via the "
            "`spl_data` property."
        ),
    )
    loader_ddr: str | None = Field(
        default=None, alias="LOADER_DDR",
        description=(
            "USB recovery only. Filename of the DDR-init blob the boot ROM "
            "expects first (rkbin's rv1106_ddr_*.bin). Not bundled — it is a "
            "vendor binary the user supplies."
        ),
    )
    loader_usbplug: str | None = Field(
        default=None, alias="LOADER_USBPLUG",
        description=(
            "USB recovery only. Filename of the second-stage blob that takes "
            "over USB and exposes flash (rkbin's rv1106_usbplug_*.bin)."
        ),
    )
    partitions: dict[str, int] = Field(
        default_factory=dict, alias="PARTITIONS",
        description=(
            "USB recovery only. Partition name to starting LBA (512-byte "
            "sectors), used to place firmware images without the caller "
            "computing offsets."
        ),
    )

    # Bytes of the SPL_BLOB, populated by the loader (not in JSON, not
    # validated by pydantic). None if `spl_blob` is unset.
    _spl_data: bytes | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _check_recovery_fields(self) -> SoCProfile:
        """Each recovery family must carry its own required fields.

        Without this the UART fields could quietly go missing on a UART chip
        and only surface as a confusing ``NoneType`` deep inside a burn.
        """
        if self.recovery == "uart":
            missing = [
                alias
                for alias, value in (
                    ("DDRSTEP0", self.ddrstep0),
                    ("ADDRESS", self.addresses),
                    ("FILELEN", self.file_lengths),
                    ("STEPLEN", self.step_lengths),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"profile '{self.name}' uses UART recovery but is missing "
                    f"{', '.join(missing)}"
                )
        return self

    def _require_uart(self, field: str) -> None:
        if self.recovery != "uart":
            raise ValueError(
                f"'{self.name}' recovers over {self.recovery}, not UART — "
                f"{field} is not defined for it"
            )

    @property
    def spl_data(self) -> bytes | None:
        """Pre-built SPL bytes if the profile declares an `SPL_BLOB`."""
        return self._spl_data

    @property
    def ddr_step_address(self) -> int:
        self._require_uart("ddr_step_address")
        assert self.addresses is not None
        return int(self.addresses[0], 16)

    @property
    def spl_address(self) -> int:
        self._require_uart("spl_address")
        assert self.addresses is not None
        return int(self.addresses[1], 16)

    @property
    def uboot_address(self) -> int:
        self._require_uart("uboot_address")
        assert self.addresses is not None
        return int(self.addresses[2], 16)

    @property
    def spl_max_size(self) -> int:
        self._require_uart("spl_max_size")
        assert self.file_lengths is not None
        return int(self.file_lengths[1], 16)

    @property
    def spl_sram_limit(self) -> int | None:
        if self.sram_limit is None:
            return None
        return int(self.sram_limit, 16)

    @property
    def ddr_step_data(self) -> bytes:
        self._require_uart("ddr_step_data")
        assert self.ddrstep0 is not None
        return bytes(self.ddrstep0)

    @property
    def prestep_data(self) -> bytes | None:
        if self.prestep0 is None:
            return None
        return bytes(self.prestep0)

    @property
    def prestep1_data(self) -> bytes | None:
        if self.prestep1 is None:
            return None
        return bytes(self.prestep1)

    model_config = {"populate_by_name": True}
