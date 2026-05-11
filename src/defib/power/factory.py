"""Power controller factory — pick implementation from environment."""

from __future__ import annotations

import os

from defib.power.base import PowerController, PowerControllerError


def power_controller_from_env() -> PowerController:
    """Build a PowerController based on the ``DEFIB_POWER_TYPE`` env var.

    - ``DEFIB_POWER_TYPE=routeros`` (default): MikroTik RouterOS API,
      configured via ``DEFIB_POE_*``.
    - ``DEFIB_POWER_TYPE=vectis``: OpenIPC Vectis UART bridge,
      configured via ``DEFIB_VECTIS_*``.
    - ``DEFIB_POWER_TYPE=rack``: rack pod HTTP API
      (``~/git/rack`` ESP32-S3 spinoff), configured via ``DEFIB_RACK_*``.

    Raises:
        PowerControllerError: if the type is unknown or required env
            vars are missing.
    """
    kind = os.environ.get("DEFIB_POWER_TYPE", "routeros").lower()
    if kind == "routeros":
        from defib.power.routeros import RouterOSController
        return RouterOSController.from_env()
    if kind == "vectis":
        from defib.power.vectis import VectisController
        return VectisController.from_env()
    if kind == "rack":
        from defib.power.rack import RackController
        return RackController.from_env()
    raise PowerControllerError(
        f"Unknown DEFIB_POWER_TYPE: {kind!r} "
        "(expected 'routeros', 'vectis', or 'rack')"
    )
