"""Pydantic models for SoC profile data."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SoCProfile(BaseModel):
    """A SoC configuration profile for the standard HiSilicon protocol.

    Fields match the JSON profile format used by the original burn tool.
    """

    name: str = Field(description="Internal chip name")
    prestep0: list[int] | None = Field(
        default=None, alias="PRESTEP0",
        description="Pre-DDR init bytecode (sent before DDRSTEP0)",
    )
    prestep1: list[int] | None = Field(
        default=None, alias="PRESTEP1",
        description="DDR training verification bytecode (sent after DDRSTEP0)",
    )
    ddrstep0: list[int] = Field(alias="DDRSTEP0", description="DDR initialization bytecode")
    addresses: list[str] = Field(
        alias="ADDRESS",
        description="Load addresses: [ddr_step, spl, uboot]",
    )
    file_lengths: list[str] = Field(
        alias="FILELEN",
        description="Size limits: [ddr_step_max, spl_max]",
    )
    step_lengths: list[str] = Field(
        alias="STEPLEN",
        description="Step frame sizes: [ddr_step, spl]",
    )

    @property
    def ddr_step_address(self) -> int:
        return int(self.addresses[0], 16)

    @property
    def spl_address(self) -> int:
        return int(self.addresses[1], 16)

    @property
    def uboot_address(self) -> int:
        return int(self.addresses[2], 16)

    @property
    def spl_max_size(self) -> int:
        return int(self.file_lengths[1], 16)

    @property
    def ddr_step_data(self) -> bytes:
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
