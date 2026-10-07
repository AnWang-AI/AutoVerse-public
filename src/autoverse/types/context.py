"""Context-compaction archive value types (docs/architecture/contracts.md)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompactionSegmentRef:
    """Stable reference to context removed from the live conversation."""

    session_id: str
    segment_id: str
    kind: str
    created_at: float
    turn_start: int
    turn_end: int
    message_count: int
    content_sha256: str


@dataclass(frozen=True)
class CompactionSlice:
    """Budgeted page of one archived segment."""

    ref: CompactionSegmentRef
    text: str
    offset: int
    next_offset: int | None
    truncated: bool


@dataclass(frozen=True)
class CompactionSearchHit:
    """Bounded search result pointing at an archived segment."""

    ref: CompactionSegmentRef
    excerpt: str
    offset: int = 0
    match_kind: str = "literal"


@dataclass(frozen=True)
class CompactionCatalogEntry:
    """Structured browse metadata for one archived segment."""

    ref: CompactionSegmentRef
    tool_names: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    error_signatures: tuple[str, ...] = ()
    preview: str = ""


@dataclass(frozen=True)
class CompactionArchiveDiagnostics:
    """Integrity state surfaced with archive tool results."""

    corrupt_records: int = 0
    indexed_segments: int = 0


__all__ = [
    "CompactionArchiveDiagnostics",
    "CompactionCatalogEntry",
    "CompactionSearchHit",
    "CompactionSegmentRef",
    "CompactionSlice",
]
