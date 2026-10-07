"""autoverse.infra.isolation — git worktree Isolator adapter (docs/reference/subagents.md §3)."""

from __future__ import annotations

from autoverse.infra.isolation.worktree import (
    GitWorktreeIsolator,
    InMemoryWorktreeRegistry,
    branch_name,
    validate_slug,
    worktree_path,
)

__all__ = [
    "GitWorktreeIsolator",
    "InMemoryWorktreeRegistry",
    "validate_slug",
    "branch_name",
    "worktree_path",
]
