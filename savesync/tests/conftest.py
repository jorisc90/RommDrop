"""Shared pytest fixtures for the savesync test suite."""

import pytest


@pytest.fixture
def tmp(tmp_path):
    """Alias for tmp_path (the legacy `tmp` fixture was removed in pytest 9)."""
    return tmp_path
