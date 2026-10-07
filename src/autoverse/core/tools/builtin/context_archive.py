"""Read-only, budgeted access to context removed by compaction."""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class SearchCompactedContextArgs(BaseModel):
    query: str = Field(..., min_length=1, description="Specific text to find in archived context.")
    max_results: int = Field(5, ge=1, le=10)


class ListCompactedContextArgs(BaseModel):
    limit: int = Field(30, ge=1, le=100)


class GrepCompactedContextArgs(BaseModel):
    pattern: str = Field(..., min_length=1)
    regex: bool = False
    segment_id: str | None = None
    max_results: int = Field(20, ge=1, le=50)


class ReadCompactedContextArgs(BaseModel):
    segment_id: str = Field(..., min_length=1)
    offset: int = Field(0, ge=0)
    max_chars: int = Field(8_000, ge=256, le=16_000)


def _archive_and_session(ctx: ToolContext) -> tuple[object | None, str]:
    archive = getattr(ctx.app, "context_archive", None)
    session_id = str(ctx.app.extra.get("session_id") or "")
    return archive, session_id


async def _diagnostic_metadata(archive: object, session_id: str) -> dict[str, object]:
    diagnostics = getattr(archive, "diagnostics", None)
    if not callable(diagnostics):
        return {}
    state = await diagnostics(session_id)
    return {
        "corrupt_records": state.corrupt_records,
        "indexed_segments": state.indexed_segments,
        "partial_results": state.corrupt_records > 0,
    }


@register_tool
class SearchCompactedContextTool(BaseTool):
    name: ClassVar[str] = "search_compacted_context"
    description: ClassVar[str] = (
        "Search this session's safely archived compacted context for a specific missing detail."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = SearchCompactedContextArgs

    async def run(
        self, args: SearchCompactedContextArgs, ctx: ToolContext
    ) -> ToolResult:
        archive, session_id = _archive_and_session(ctx)
        search = getattr(archive, "search", None)
        if not session_id or not callable(search):
            return ToolResult.error(
                "Compacted-context archive is unavailable for this session.",
                error_kind="archive_unavailable",
            )
        try:
            hits = await search(session_id, args.query, max_results=args.max_results)
        except ValueError as exc:
            return ToolResult.error(str(exc), error_kind="invalid_query")
        rows = [
            {
                "segment_id": hit.ref.segment_id,
                "kind": hit.ref.kind,
                "turn_start": hit.ref.turn_start,
                "turn_end": hit.ref.turn_end,
                "excerpt": hit.excerpt,
                "offset": hit.offset,
                "match_kind": hit.match_kind,
            }
            for hit in hits
        ]
        metadata = await _diagnostic_metadata(archive, session_id)
        return ToolResult.ok(
            json.dumps(rows, ensure_ascii=False, indent=2) if rows else "No archived match.",
            match_count=len(rows),
            untrusted_historical_content=True,
            **metadata,
        )


@register_tool
class ListCompactedContextTool(BaseTool):
    name: ClassVar[str] = "list_compacted_context"
    description: ClassVar[str] = (
        "Browse structured metadata for this session's compacted-context segments."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = ListCompactedContextArgs

    async def run(
        self, args: ListCompactedContextArgs, ctx: ToolContext
    ) -> ToolResult:
        archive, session_id = _archive_and_session(ctx)
        list_catalog = getattr(archive, "list_catalog", None)
        if not session_id or not callable(list_catalog):
            return ToolResult.error(
                "Compacted-context archive is unavailable for this session.",
                error_kind="archive_unavailable",
            )
        entries = await list_catalog(session_id, limit=args.limit)
        rows = [
            {
                "segment_id": entry.ref.segment_id,
                "kind": entry.ref.kind,
                "turn_start": entry.ref.turn_start,
                "turn_end": entry.ref.turn_end,
                "message_count": entry.ref.message_count,
                "tool_names": entry.tool_names,
                "paths": entry.paths,
                "commands": entry.commands,
                "error_signatures": entry.error_signatures,
                "preview": entry.preview,
            }
            for entry in entries
        ]
        metadata = await _diagnostic_metadata(archive, session_id)
        return ToolResult.ok(
            json.dumps(rows, ensure_ascii=False, indent=2) if rows else "No compacted context.",
            segment_count=len(rows),
            untrusted_historical_content=True,
            **metadata,
        )


@register_tool
class GrepCompactedContextTool(BaseTool):
    name: ClassVar[str] = "grep_compacted_context"
    description: ClassVar[str] = (
        "Run deterministic literal or regular-expression matching over compacted context."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = GrepCompactedContextArgs

    async def run(
        self, args: GrepCompactedContextArgs, ctx: ToolContext
    ) -> ToolResult:
        archive, session_id = _archive_and_session(ctx)
        grep = getattr(archive, "grep", None)
        if not session_id or not callable(grep):
            return ToolResult.error(
                "Compacted-context archive is unavailable for this session.",
                error_kind="archive_unavailable",
            )
        try:
            hits = await grep(
                session_id,
                args.pattern,
                regex=args.regex,
                segment_id=args.segment_id,
                max_results=args.max_results,
            )
        except ValueError as exc:
            return ToolResult.error(str(exc), error_kind="invalid_pattern")
        rows = [
            {
                "segment_id": hit.ref.segment_id,
                "turn_start": hit.ref.turn_start,
                "turn_end": hit.ref.turn_end,
                "offset": hit.offset,
                "match_kind": hit.match_kind,
                "excerpt": hit.excerpt,
            }
            for hit in hits
        ]
        metadata = await _diagnostic_metadata(archive, session_id)
        return ToolResult.ok(
            json.dumps(rows, ensure_ascii=False, indent=2) if rows else "No archived match.",
            match_count=len(rows),
            untrusted_historical_content=True,
            **metadata,
        )


@register_tool
class ReadCompactedContextTool(BaseTool):
    name: ClassVar[str] = "read_compacted_context"
    description: ClassVar[str] = (
        "Read one bounded page of a compacted-context segment from this session."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = ReadCompactedContextArgs

    async def run(
        self, args: ReadCompactedContextArgs, ctx: ToolContext
    ) -> ToolResult:
        archive, session_id = _archive_and_session(ctx)
        read = getattr(archive, "read", None)
        if not session_id or not callable(read):
            return ToolResult.error(
                "Compacted-context archive is unavailable for this session.",
                error_kind="archive_unavailable",
            )
        try:
            page = await read(
                session_id,
                args.segment_id,
                offset=args.offset,
                max_chars=args.max_chars,
            )
        except KeyError:
            metadata = await _diagnostic_metadata(archive, session_id)
            corrupt_records = metadata.get("corrupt_records", 0)
            if isinstance(corrupt_records, int) and corrupt_records > 0:
                return ToolResult.error(
                    "Segment was not readable and the archive contains corrupt records.",
                    error_kind="archive_corrupt",
                    **metadata,
                )
            return ToolResult.error(
                f"Compacted-context segment not found: {args.segment_id}",
                error_kind="not_found",
            )
        except ValueError as exc:
            return ToolResult.error(str(exc), error_kind="integrity_error")
        metadata = await _diagnostic_metadata(archive, session_id)
        return ToolResult.ok(
            page.text,
            segment_id=page.ref.segment_id,
            offset=page.offset,
            next_offset=page.next_offset,
            truncated=page.truncated,
            untrusted_historical_content=True,
            **metadata,
        )


__all__ = [
    "GrepCompactedContextArgs",
    "GrepCompactedContextTool",
    "ListCompactedContextArgs",
    "ListCompactedContextTool",
    "ReadCompactedContextArgs",
    "ReadCompactedContextTool",
    "SearchCompactedContextArgs",
    "SearchCompactedContextTool",
]
