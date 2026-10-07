"""Tests for budgeted compacted-context retrieval tools."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from typing import Any, cast

from autoverse.core.tools.builtin.context_archive import (
    GrepCompactedContextArgs,
    GrepCompactedContextTool,
    ListCompactedContextArgs,
    ListCompactedContextTool,
    ReadCompactedContextArgs,
    ReadCompactedContextTool,
    SearchCompactedContextArgs,
    SearchCompactedContextTool,
)
from autoverse.core.tools.context import ToolContext
from autoverse.infra.persist.compaction_archive import (
    FileCompactionArchive,
    InMemoryCompactionArchive,
)
from autoverse.types import Message, TextBlock


async def test_search_then_read_is_bounded_and_session_scoped() -> None:
    archive = InMemoryCompactionArchive()
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(
            Message(role="user", content=(TextBlock(text="A" * 500 + " needle"),)),
        ),
        turn_start=1,
        turn_end=1,
    )
    ctx = cast(
        ToolContext,
        cast(Any, SimpleNamespace(app=SimpleNamespace(
            context_archive=archive, extra={"session_id": "s1"}
        ))),
    )
    search = await SearchCompactedContextTool().run(
        SearchCompactedContextArgs(query="needle"), ctx
    )
    assert search.succeeded and ref.segment_id in search.output
    page = await ReadCompactedContextTool().run(
        ReadCompactedContextArgs(segment_id=ref.segment_id, max_chars=256), ctx
    )
    assert page.succeeded
    assert page.metadata["truncated"] is True
    assert page.metadata["untrusted_historical_content"] is True

    ctx.app.extra["session_id"] = "s2"
    denied = await ReadCompactedContextTool().run(
        ReadCompactedContextArgs(segment_id=ref.segment_id), ctx
    )
    assert denied.is_error and denied.error_kind == "not_found"


async def test_list_and_grep_support_model_driven_archive_investigation() -> None:
    archive = InMemoryCompactionArchive()
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="FAILED exact-code-42"),)),),
        turn_start=3,
        turn_end=3,
    )
    ctx = cast(
        ToolContext,
        cast(
            Any,
            SimpleNamespace(
                app=SimpleNamespace(
                    context_archive=archive, extra={"session_id": "s1"}
                )
            ),
        ),
    )
    listed = await ListCompactedContextTool().run(ListCompactedContextArgs(), ctx)
    assert listed.succeeded and ref.segment_id in listed.output
    grepped = await GrepCompactedContextTool().run(
        GrepCompactedContextArgs(pattern=r"exact-code-\d+", regex=True), ctx
    )
    assert grepped.succeeded and ref.segment_id in grepped.output


async def test_tools_surface_partial_results_for_corrupt_archive(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="known marker"),)),),
        turn_start=0,
        turn_end=0,
    )
    with (tmp_path / "s1" / "segments.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    ctx = cast(
        ToolContext,
        cast(
            Any,
            SimpleNamespace(
                app=SimpleNamespace(
                    context_archive=archive, extra={"session_id": "s1"}
                )
            ),
        ),
    )
    result = await SearchCompactedContextTool().run(
        SearchCompactedContextArgs(query="known marker"), ctx
    )
    assert result.succeeded
    assert result.metadata["partial_results"] is True
    assert result.metadata["corrupt_records"] == 1


async def test_grep_reports_invalid_regex() -> None:
    archive = InMemoryCompactionArchive()
    ctx = cast(
        ToolContext,
        cast(
            Any,
            SimpleNamespace(
                app=SimpleNamespace(
                    context_archive=archive, extra={"session_id": "s1"}
                )
            ),
        ),
    )
    result = await GrepCompactedContextTool().run(
        GrepCompactedContextArgs(pattern="[", regex=True), ctx
    )
    assert result.is_error and result.error_kind == "invalid_pattern"
