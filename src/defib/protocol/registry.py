"""Protocol plugin registry.

Protocols register themselves via the @register decorator.
External protocols can register via entry points under the
"defib.protocols" group.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Type

from defib.protocol.base import BootProtocol

logger = logging.getLogger(__name__)

_registry: list[Type[BootProtocol]] = []
_entry_points_loaded = False


def register(cls: Type[BootProtocol]) -> Type[BootProtocol]:
    """Decorator to register a protocol implementation."""
    if cls not in _registry:
        _registry.append(cls)
    return cls


def _load_entry_points() -> None:
    """Load protocol plugins from entry points (once)."""
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    _entry_points_loaded = True

    try:
        eps = importlib.metadata.entry_points()
        protocol_eps = eps.select(group="defib.protocols") if hasattr(eps, "select") else []
        for ep in protocol_eps:
            try:
                ep.load()  # Loading the module triggers @register
            except Exception:
                logger.warning("Failed to load protocol plugin: %s", ep.name, exc_info=True)
    except Exception:
        logger.debug("No entry points available", exc_info=True)


def find_protocol(chip_name: str) -> Type[BootProtocol]:
    """Find the protocol implementation that handles the given chip.

    Raises:
        ValueError: If no protocol matches the chip name.
    """
    _load_entry_points()

    # Strip any ``:variant`` suffix — protocol selection keys on base chip
    # (variant overrides DDR/SPL details, not the boot protocol family).
    chip_lower = chip_name.lower().split(":", 1)[0]
    for cls in _registry:
        if cls.matches(chip_lower):
            return cls
    raise ValueError(f"No protocol found for chip: {chip_name}")


def list_protocols() -> list[Type[BootProtocol]]:
    """Return all registered protocol implementations."""
    _load_entry_points()
    return list(_registry)
