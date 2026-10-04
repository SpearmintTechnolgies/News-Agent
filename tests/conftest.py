"""Shared pytest fixtures for NewsAgent V5.

Keeps the default test suite offline: live RSS source expansion is skipped
unless a test explicitly clears NEWSAGENT_V5_SKIP_SOURCE_EXPANSION.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_offline_source_expansion(monkeypatch):
    """Prevent accidental live feed fetches during unit/integration tests."""
    monkeypatch.setenv("NEWSAGENT_V5_SKIP_SOURCE_EXPANSION", "true")
