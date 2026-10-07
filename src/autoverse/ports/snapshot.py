"""SnapshotPort — workspace snapshot abstraction (docs/reference/tools.md seam).

A SnapshotPort captures and restores the state of the workspace's files. It is
deliberately free of any worktree logic (that is the Isolator's concern) and of
turn-boundary / `/rewind` wiring. core depends only on this Protocol; the
GitShadowSnapshot adapter lives in infra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SnapshotRef:
    """An opaque reference to a captured snapshot."""

    hash: str
    created_at: str
    files: tuple[str, ...]


@runtime_checkable
class SnapshotPort(Protocol):
    async def track(self) -> SnapshotRef | None:
        """Capture current state. Returns None if there is nothing new to track."""
        ...

    async def diff(self, ref: SnapshotRef) -> str:
        """Return a unified diff between `ref` and the current state."""
        ...

    async def restore(self, ref: SnapshotRef) -> tuple[str, ...]:
        """Restore files to `ref`; returns the paths that were changed."""
        ...


__all__ = ["SnapshotRef", "SnapshotPort"]
