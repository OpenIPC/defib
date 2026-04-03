"""Event types emitted by recovery sessions for UI consumption."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    POWER_CYCLE = "power_cycle"
    HANDSHAKE = "handshake"
    DDR_INIT = "ddr_init"
    SPL = "spl"
    UBOOT = "uboot"
    GSL = "gsl"
    DDR_TABLE = "ddr_table"
    DDR_TRAINING = "ddr_training"
    HEAD_AREA = "head_area"
    AUX_AREA = "aux_area"
    BOOT_IMAGE = "boot_image"
    BOARD_ID = "board_id"
    COMPLETE = "complete"


@dataclass
class HandshakeResult:
    success: bool
    chip_id: int | None = None
    board_id: int | None = None
    cpu_id: int | None = None
    message: str = ""


@dataclass
class ProgressEvent:
    stage: Stage
    bytes_sent: int
    bytes_total: int
    message: str | None = None

    @property
    def percent(self) -> float:
        if self.bytes_total == 0:
            return 100.0
        return (self.bytes_sent / self.bytes_total) * 100.0


@dataclass
class LogEvent:
    level: str  # "debug", "info", "warn", "error"
    message: str
    raw_data: bytes | None = None


@dataclass
class RecoveryResult:
    success: bool
    stages_completed: list[Stage] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0
