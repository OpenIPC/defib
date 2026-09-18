"""Input model for the OpenIPC installer."""

from __future__ import annotations

from dataclasses import dataclass


INSTALL_STAGE_ORDER = (
    "uboot",
    "kernel",
    "rootfs",
    "rootfs-data",
    "env",
    "reset",
)


def resolve_install_stages(
    selected: tuple[str, ...] = (),
    skipped: tuple[str, ...] = (),
    *,
    final_reset: bool = True,
) -> tuple[str, ...]:
    """Return a validated install-stage plan in execution order.

    With no explicit selection the historic production flow is preserved.  A
    repeated ``--stage`` option switches to an exact allow-list, while repeated
    ``--skip-stage`` options subtract from the production flow.  Keeping these
    modes mutually exclusive avoids ambiguous destructive plans.
    """
    known = set(INSTALL_STAGE_ORDER)
    selected_set = {stage.strip().lower() for stage in selected if stage.strip()}
    skipped_set = {stage.strip().lower() for stage in skipped if stage.strip()}

    if selected_set and skipped_set:
        raise ValueError("--stage and --skip-stage cannot be used together")

    unknown = (selected_set | skipped_set) - known
    if unknown:
        names = ", ".join(sorted(unknown))
        valid = ", ".join(INSTALL_STAGE_ORDER)
        raise ValueError(f"unknown install stage(s): {names}; valid stages: {valid}")

    if selected_set:
        # Exact selection: reset only happens when explicitly requested.  This
        # makes e.g. ``--stage env`` safe for iterative U-Boot debugging.
        if not final_reset and "reset" in selected_set:
            raise ValueError("--stage reset conflicts with --no-final-reset")
        active = selected_set
    else:
        active = known - skipped_set
        if not final_reset:
            active.discard("reset")

    if not active:
        raise ValueError("install stage selection is empty")

    return tuple(stage for stage in INSTALL_STAGE_ORDER if stage in active)


@dataclass(frozen=True)
class InstallRequest:
    """Validated CLI inputs consumed by install orchestration."""

    chip: str
    firmware_path: str
    uboot_path: str = ""
    port: str = "/dev/ttyUSB0"
    power_cycle: bool = False
    poe_port_override: str = ""
    nic: str = ""
    host_ip: str = "192.168.1.10"
    device_ip: str = "192.168.1.20"
    tftp_port: int = 69
    nor_size: int = 0
    nand: bool = False
    wipe_env: bool = False
    wipe_rootfs_data: bool = False
    final_reset: bool = True
    stages: tuple[str, ...] = ()
    skip_stages: tuple[str, ...] = ()
    tftp_via: str = "auto"
    output: str = "human"
    debug: bool = False
