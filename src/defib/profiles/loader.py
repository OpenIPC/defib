"""SoC profile loading with alias chain resolution."""

from __future__ import annotations

import json
import logging
from importlib import resources
from pathlib import Path

from defib.profiles.schema import SoCProfile

logger = logging.getLogger(__name__)

MAX_ALIAS_DEPTH = 23


def _get_profiles_dir() -> Path:
    """Get the path to the bundled profiles/data directory."""
    ref = resources.files("defib.profiles") / "data"
    # resources.files returns a Traversable; for file-based installs it's a Path
    return Path(str(ref))


def load_profile(chip_name: str, profiles_dir: Path | None = None) -> SoCProfile:
    """Load a SoC profile by chip name, resolving alias chains.

    Args:
        chip_name: The chip identifier (e.g., "hi3516cv300").
        profiles_dir: Optional override for the profiles directory.

    Returns:
        Parsed SoCProfile.

    Raises:
        FileNotFoundError: If the profile JSON doesn't exist.
        ValueError: If alias chain exceeds MAX_ALIAS_DEPTH.
    """
    if profiles_dir is None:
        profiles_dir = _get_profiles_dir()

    current = chip_name.lower()
    for _ in range(MAX_ALIAS_DEPTH):
        profile_path = profiles_dir / f"{current}.json"
        if not profile_path.exists():
            raise FileNotFoundError(f"No profile found for chip: {current}")

        content = profile_path.read_text().strip()

        # Check if this is an alias (single token ending in .json)
        tokens = content.split()
        if len(tokens) == 1 and tokens[0].endswith(".json"):
            current = tokens[0][:-5]  # Strip .json and follow the alias
            continue

        data = json.loads(content)
        return SoCProfile.model_validate(data)

    raise ValueError(f"Alias chain too deep for chip: {chip_name}")


def list_chips(profiles_dir: Path | None = None) -> list[str]:
    """List all available chip names from profile files.

    Returns chip names sorted alphabetically.
    """
    if profiles_dir is None:
        profiles_dir = _get_profiles_dir()

    chips: list[str] = []
    if profiles_dir.exists():
        for path in profiles_dir.glob("*.json"):
            chips.append(path.stem)

    return sorted(chips)


def list_all_chips(profiles_dir: Path | None = None) -> list[str]:
    """List all chip names including hardcoded protocol-specific ones."""
    from defib.protocol.hisilicon_v500 import V500_SOCS
    from defib.protocol.hisilicon_cv6xx import CV6XX_SOCS

    chips = set(list_chips(profiles_dir))
    chips.update(V500_SOCS)
    chips.update(CV6XX_SOCS)
    return sorted(chips)
