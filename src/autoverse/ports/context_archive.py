"""Port for durable, budgeted retrieval of compacted conversation context."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from autoverse.types import Message
from autoverse.types.context import (
    CompactionArchiveDiagnostics,
    CompactionCatalogEntry,
    CompactionSearchHit,
    CompactionSegmentRef,
    CompactionSlice,
)


@runtime_checkable
class CompactionArchivePort(Protocol):
    """Persistence boundary used before conversation history is discarded."""

    async def append(
        self,
        session_id: str,
        *,
        kind: str,
        messages: tuple[Message, ...],
        turn_start: int,
        turn_end: int,
    ) -> CompactionSegmentRef: ...

    async def read(
        self,
        session_id: str,
        segment_id: str,
        *,
        offset: int = 0,
        max_chars: int = 8_000,
    ) -> CompactionSlice: ...

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        max_results: int = 5,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]: ...

    async def grep(
        self,
        session_id: str,
        pattern: str,
        *,
        regex: bool = False,
        segment_id: str | None = None,
        max_results: int = 20,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]: ...

    async def list_catalog(
        self, session_id: str, *, limit: int = 50
    ) -> tuple[CompactionCatalogEntry, ...]: ...

    async def list_refs(
        self, session_id: str
    ) -> tuple[CompactionSegmentRef, ...]: ...

    async def diagnostics(
        self, session_id: str
    ) -> CompactionArchiveDiagnostics: ...


__all__ = ["CompactionArchivePort"]
