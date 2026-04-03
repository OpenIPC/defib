"""Abstract base class for power controllers."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class PowerControllerError(Exception):
    """Base exception for power controller errors."""


class PowerController(ABC):
    """Abstract base class for device power controllers.

    Implementations control power to a device (e.g., PoE switch port,
    smart PDU outlet, relay board).
    """

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Human-readable controller name."""

    @abstractmethod
    async def power_off(self, port: str) -> None:
        """Turn off power to the specified port/outlet."""

    @abstractmethod
    async def power_on(self, port: str) -> None:
        """Turn on power to the specified port/outlet."""

    async def power_cycle(self, port: str, off_duration: float = 3.0) -> None:
        """Power cycle: off, wait, on."""
        await self.power_off(port)
        await asyncio.sleep(off_duration)
        await self.power_on(port)

    @abstractmethod
    async def close(self) -> None:
        """Release any resources (TCP connections, etc.)."""
