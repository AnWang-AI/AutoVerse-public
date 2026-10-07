"""Session navigation catalog contracts.

The catalog owns small, frequently-mutated navigation fields while transcript
messages remain owned by :class:`Store`.  Projection rows are rebuildable from
the Store; ``pinned`` and ``last_viewed_at`` are catalog-owned user state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from autoverse.ports.query import SessionSummary


@dataclass(frozen=True)
class SessionProjection:
    """Rebuildable session-list fields derived from Store messages and meta."""

    session_id: str
    title: str = ""
    model: str = ""
    cwd: str = ""
    group_root: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    message_count: int = 0
    last_activity_at: float = 0.0
    last_outcome: str = ""
    source_mtime_ns: int = 0
    legacy_last_viewed_at: float = 0.0
    legacy_pinned: bool = False


@runtime_checkable
class SessionCatalogPort(Protocol):
    """Indexed navigation state shared by frontends and app assembly."""

    async def list(
        self, *, cwd: str | None = None, limit: int | None = None
    ) -> list[SessionSummary]:
        """Return newest summaries, optionally indexed by cwd and bounded."""
        ...

    async def get(self, session_id: str) -> SessionSummary | None:
        """Return one current summary, or ``None`` when it is not indexed."""
        ...

    async def source_versions(self) -> dict[str, int]:
        """Return projection source mtimes for startup repair decisions."""
        ...

    async def upsert_projection(self, projection: SessionProjection) -> None:
        """Replace rebuildable fields without overwriting catalog-owned state."""
        ...

    async def mark_viewed(self, session_id: str, viewed_at: float) -> None:
        """Monotonically advance the catalog-owned view timestamp."""
        ...

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        """Set the catalog-owned pinned flag."""
        ...

    async def rename(self, session_id: str, title: str) -> None:
        """Update the indexed title after the Store rename succeeds."""
        ...

    async def delete(self, session_id: str) -> None:
        """Delete only the navigation row, never transcript data."""
        ...


__all__ = ["SessionCatalogPort", "SessionProjection"]
