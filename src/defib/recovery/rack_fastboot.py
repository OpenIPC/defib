"""Pod-side fastboot bring-up for rack-pod-controlled cameras.

When a rack pod is the power controller, the host can't reliably drive
the HiSilicon SPL boot protocol over its WiFi-bridged UART — the
per-frame ACK loop (150 ms timeout × dozens of frames per upload)
doesn't survive WiFi RTT. Instead the pod runs the entire upload
sequence locally on its UART and returns a phase-by-phase JSON
summary; this module is the host-side adapter that:

1. Loads the SoC profile (PRESTEP0 / DDRSTEP0 / optional PRESTEP1 +
   load addresses).
2. Detects the SPL boundary in the firmware blob with the same logic
   `HiSiliconStandard._send_spl` uses on the host path, then zeroes
   long 0xFF runs (cv500-family bootrom RX bug).
3. Calls :meth:`RackController.fastboot` with the assembled bundle.
4. Returns a :class:`RecoveryResult`-shaped object so callers can drop
   it into the same post-burn flow they use for `session.run()`.

The CLI's burn / install / agent-upload paths use this when
``power_controller`` is a :class:`RackController`. Note that the pod
takes exclusive UART access during the upload, so callers MUST NOT
have a TCP client connected to ``tcp://<pod>:9000`` when this runs —
open the transport only after this function returns.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from defib.power.rack import RackController
from defib.profiles.loader import load_profile
from defib.protocol.hisilicon_standard import HiSiliconStandard
from defib.recovery.events import RecoveryResult, Stage

logger = logging.getLogger(__name__)


async def run_rack_fastboot(
    rack: RackController,
    chip: str,
    firmware: bytes | str | Path,
    agent_payload: bytes | None = None,
    timeout: float = 180.0,
) -> RecoveryResult:
    """Run the pod-side SPL/DDR/U-Boot upload via ``POST /fastboot``.

    Args:
        rack: configured :class:`RackController`.
        chip: SoC name (e.g. ``"hi3516ev300"``) for profile lookup.
        firmware: u-boot bytes (or path) used to derive the SPL portion
            and — if ``agent_payload`` is omitted — as the U-Boot blob
            loaded at ``profile.uboot_address``.  Matches the host-path
            behaviour of ``defib burn``: the same binary contains both
            the SPL boundary and the U-Boot to run.
        agent_payload: optional override for the blob loaded at
            ``profile.uboot_address``.  Used by the agent-flash path,
            which sends ``u-boot.bin`` as SPL and the flash agent as
            U-Boot.  ``None`` (the default) uses ``firmware`` for both.
        timeout: HTTP timeout for the fastboot POST.

    Returns:
        :class:`RecoveryResult` with ``success`` / ``elapsed_ms`` /
        ``error`` populated.  ``stages_completed`` reflects the phases
        the pod reported reaching.
    """
    firmware_bytes = (
        firmware if isinstance(firmware, (bytes, bytearray))
        else Path(firmware).read_bytes()
    )
    profile = load_profile(chip)

    # Same SPL-boundary detection the host path uses — keep both paths
    # byte-identical so a chip that works on one works on the other.
    scan_buf = agent_payload if agent_payload is not None else firmware_bytes
    spl_size = HiSiliconStandard._detect_spl_size(
        scan_buf, profile.spl_max_size, sram_limit=profile.spl_sram_limit,
    )
    spl_bytes = firmware_bytes[:spl_size].ljust(spl_size, b"\x00")
    spl_bytes = HiSiliconStandard._zero_long_ff_runs(spl_bytes)
    uboot_bytes = agent_payload if agent_payload is not None else firmware_bytes

    logger.info(
        "rack fastboot: spl=%d agent=%d profile=%s spl_addr=0x%x ddr_addr=0x%x uboot_addr=0x%x",
        len(spl_bytes), len(uboot_bytes), profile.name,
        profile.spl_address, profile.ddr_step_address, profile.uboot_address,
    )

    t0 = time.monotonic()
    response = await rack.fastboot(
        spl_address=profile.spl_address,
        ddr_step_address=profile.ddr_step_address,
        uboot_address=profile.uboot_address,
        prestep0=profile.prestep_data or b"",
        ddrstep0=profile.ddr_step_data,
        prestep1=profile.prestep1_data,
        spl=spl_bytes,
        agent=uboot_bytes,
        timeout=timeout,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    stages: list[Stage] = []
    last_phase = str(response.get("last_phase", ""))
    # Pod's phase names map onto defib's Stage enum where they exist.
    if last_phase in ("frame_for_start", "prestep0", "ddrstep0", "prestep1", "spl", "agent", "done"):
        stages.append(Stage.HANDSHAKE)
    if last_phase in ("prestep0", "ddrstep0", "prestep1", "spl", "agent", "done"):
        stages.append(Stage.DDR_INIT)
    if last_phase in ("spl", "agent", "done"):
        stages.append(Stage.SPL)
    if last_phase in ("agent", "done"):
        stages.append(Stage.UBOOT)
    if last_phase == "done":
        stages.append(Stage.COMPLETE)

    if response.get("success"):
        return RecoveryResult(
            success=True, stages_completed=stages, elapsed_ms=elapsed_ms,
        )

    failed = response.get("failed_phase", "unknown")
    err = response.get("error", "unknown")
    return RecoveryResult(
        success=False, stages_completed=stages,
        error=f"rack fastboot failed at {failed}: {err}",
        elapsed_ms=elapsed_ms,
    )
