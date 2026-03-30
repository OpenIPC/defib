"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from defib.transport.mock import MockTransport

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PROFILES_DIR = Path(__file__).parent.parent / "src" / "defib" / "profiles" / "data"


@pytest.fixture
def mock_transport() -> MockTransport:
    """Fresh mock transport for testing."""
    return MockTransport()


@pytest.fixture
def profiles_dir() -> Path:
    return PROFILES_DIR
