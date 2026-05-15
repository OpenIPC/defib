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


def parse_chip_variant(chip: str) -> tuple[str, str | None]:
    """Split an optional ``chip:variant`` form into its parts.

    ``hi3516av300`` → ``("hi3516av300", None)``
    ``hi3516av300:emmc`` → ``("hi3516av300", "emmc")``

    Used by every helper that accepts a chip identifier so callers can
    pass the joined form consistently without each layer re-parsing.
    """
    if ":" in chip:
        base, variant = chip.split(":", 1)
        return base.lower(), variant.lower() or None
    return chip.lower(), None


def load_profile(chip_name: str, profiles_dir: Path | None = None) -> SoCProfile:
    """Load a SoC profile by chip name, resolving alias chains and variants.

    ``chip_name`` may optionally use the ``chip:variant`` form (e.g.
    ``hi3516av300:emmc``). When a variant is specified, the loader applies
    the variant's per-board overrides (typically DDRSTEP0 / PRESTEP0) on
    top of the base profile.

    Args:
        chip_name: The chip identifier (e.g., ``hi3516cv300`` or
            ``hi3516av300:emmc``).
        profiles_dir: Optional override for the profiles directory.

    Returns:
        Parsed SoCProfile with variant overrides applied if any.

    Raises:
        FileNotFoundError: If the profile JSON doesn't exist.
        ValueError: If the alias chain is too deep, or the requested
            variant isn't declared for this chip.
    """
    if profiles_dir is None:
        profiles_dir = _get_profiles_dir()

    base_chip, variant = parse_chip_variant(chip_name)

    current = base_chip
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
        # Pop variants before pydantic sees the dict so the SoCProfile
        # model itself stays variant-unaware.
        variants = data.pop("variants", None) or {}
        if variant is not None:
            if variant not in variants:
                available = sorted(variants) if variants else []
                avail_str = ", ".join(available) if available else "(none declared)"
                raise ValueError(
                    f"Unknown variant '{variant}' for chip '{current}'. "
                    f"Available variants: {avail_str}"
                )
            # Variant entries override matching top-level keys
            data.update(variants[variant])
        profile = SoCProfile.model_validate(data)

        # Resolve SPL_BLOB if declared. Path is relative to the profile JSON's
        # directory. Done here (not in pydantic) so the schema stays I/O-free.
        if profile.spl_blob:
            blob_path = profile_path.parent / profile.spl_blob
            if not blob_path.exists():
                raise FileNotFoundError(
                    f"SPL_BLOB '{profile.spl_blob}' for chip '{current}' not "
                    f"found at {blob_path}"
                )
            profile._spl_data = blob_path.read_bytes()
        return profile

    raise ValueError(f"Alias chain too deep for chip: {chip_name}")


def list_variants(chip_name: str, profiles_dir: Path | None = None) -> list[str]:
    """Return the list of board variants declared for ``chip_name``.

    Empty list when the chip has no variants. Aliases are followed.
    Variant suffixes in ``chip_name`` (after ``:``) are stripped first.
    """
    if profiles_dir is None:
        profiles_dir = _get_profiles_dir()

    base_chip, _ = parse_chip_variant(chip_name)
    current = base_chip
    for _ in range(MAX_ALIAS_DEPTH):
        profile_path = profiles_dir / f"{current}.json"
        if not profile_path.exists():
            return []
        content = profile_path.read_text().strip()
        tokens = content.split()
        if len(tokens) == 1 and tokens[0].endswith(".json"):
            current = tokens[0][:-5]
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []
        return sorted((data.get("variants") or {}).keys())
    return []


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
