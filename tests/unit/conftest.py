"""Shared fixtures for Phase 4 unit tests (subagent + isolation)."""

from __future__ import annotations

from pathlib import Path

import pytest

import autoverse.core.tools.builtin  # noqa: F401 — trigger tool registration
from tests._doubles import make_git_repo


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one initial commit."""
    return make_git_repo(tmp_path)
