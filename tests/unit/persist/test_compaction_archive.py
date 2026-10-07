"""Tests for the durable compacted-context archive."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.infra.observ.redact import REDACTED, Redactor
from autoverse.infra.persist.compaction_archive import FileCompactionArchive
from autoverse.types import Message, TextBlock, ToolResultBlock, ToolUseBlock


async def test_archive_round_trip_search_and_pagination(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    messages = (
        Message(
            role="assistant",
            content=(ToolUseBlock(id="t1", name="bash", input={"command": "pytest"}),),
        ),
        Message(
            role="user",
            content=(ToolResultBlock(tool_use_id="t1", content="A" * 20_000 + " UNIQUE"),),
        ),
    )
    ref = await archive.append(
        "s1", kind="micro", messages=messages, turn_start=2, turn_end=3
    )

    hits = await archive.search("s1", "UNIQUE")
    assert hits and hits[0].ref == ref
    first = await archive.read("s1", ref.segment_id, max_chars=1_000)
    assert first.truncated and first.next_offset == 1_000
    second = await archive.read(
        "s1", ref.segment_id, offset=first.next_offset, max_chars=30_000
    )
    assert "UNIQUE" in second.text
    assert list((tmp_path / "s1" / "blobs").glob("*.txt"))


async def test_archive_redacts_nested_values_and_is_session_scoped(tmp_path: Path) -> None:
    secret = "literal-private-value"
    archive = FileCompactionArchive(tmp_path, redactor=Redactor([secret]))
    message = Message(
        role="user",
        content=(
            TextBlock(text=f"value={secret}"),
            ToolUseBlock(
                id="t1",
                name="web",
                input={"nested": [{"password": "tiny"}, secret]},
            ),
        ),
        metadata={"authorization": secret},
    )
    ref = await archive.append(
        "s1", kind="overflow", messages=(message,), turn_start=0, turn_end=0
    )
    page = await archive.read("s1", ref.segment_id)
    assert secret not in page.text
    assert REDACTED in page.text
    assert secret not in "".join(
        path.read_text(errors="ignore") for path in tmp_path.rglob("*") if path.is_file()
    )
    with pytest.raises(KeyError):
        await archive.read("s2", ref.segment_id)


async def test_archive_skips_corrupt_jsonl_line(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="kept"),)),),
        turn_start=0,
        turn_end=0,
    )
    with (tmp_path / "s1" / "segments.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{bad json\n")
    assert (await archive.list_refs("s1")) == (ref,)
    diagnostics = await archive.diagnostics("s1")
    assert diagnostics.corrupt_records == 1


async def test_archive_rejects_session_path_traversal(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    with pytest.raises(ValueError):
        await archive.list_refs("../outside")


async def test_fts_search_handles_token_order_and_unicode_normalisation(
    tmp_path: Path,
) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="summary",
        messages=(
            Message(
                role="user",
                content=(TextBlock(text="Run pytest on café module unit tests"),),
            ),
        ),
        turn_start=2,
        turn_end=2,
    )
    reordered = await archive.search("s1", "unit pytest module")
    assert reordered and reordered[0].ref == ref
    assert reordered[0].match_kind == "fts_and"
    normalised = await archive.search("s1", "ＣＡＦÉ")
    assert normalised and normalised[0].ref == ref


async def test_literal_search_ranks_more_specific_match_before_recency(
    tmp_path: Path,
) -> None:
    archive = FileCompactionArchive(tmp_path)
    relevant = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="needle needle needle"),)),),
        turn_start=1,
        turn_end=1,
    )
    await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="newer needle"),)),),
        turn_start=2,
        turn_end=2,
    )
    hits = await archive.search("s1", "needle", max_results=1)
    assert hits[0].ref == relevant


async def test_exact_identifier_is_ranked_before_prefix_matches(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    exact = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="unique-17"),)),),
        turn_start=1,
        turn_end=1,
    )
    for suffix in range(170, 180):
        await archive.append(
            "s1",
            kind="micro",
            messages=(
                Message(role="user", content=(TextBlock(text=f"unique-{suffix}"),)),
            ),
            turn_start=2,
            turn_end=2,
        )
    hits = await archive.search("s1", "unique-17", max_results=1)
    assert hits[0].ref == exact


async def test_catalog_and_grep_are_deterministic(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(
            Message(
                role="assistant",
                content=(
                    ToolUseBlock(
                        id="t1",
                        name="bash",
                        input={"command": "pytest tests/unit/test_app.py -q"},
                    ),
                ),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_use_id="t1",
                        content="FAILED tests/unit/test_app.py::test_case AssertionError",
                    ),
                ),
            ),
        ),
        turn_start=4,
        turn_end=4,
    )
    catalog = await archive.list_catalog("s1")
    assert catalog[0].ref == ref
    assert catalog[0].tool_names == ("bash",)
    assert catalog[0].commands == ("pytest tests/unit/test_app.py -q",)
    assert any("test_app.py" in path for path in catalog[0].paths)
    hits = await archive.grep("s1", r"FAILED .*AssertionError", regex=True)
    assert hits and hits[0].ref == ref and hits[0].match_kind == "regex"


async def test_search_uses_index_without_loading_unrelated_blob(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(
            Message(
                role="user",
                content=(ToolResultBlock(tool_use_id="t1", content="marker " + "x" * 20_000),),
            ),
        ),
        turn_start=1,
        turn_end=1,
    )
    for blob in (tmp_path / "s1" / "blobs").glob("*.txt"):
        blob.unlink()
    hits = await archive.search("s1", "marker")
    assert hits and hits[0].ref == ref
    with pytest.raises(ValueError, match="unreadable"):
        await archive.read("s1", ref.segment_id)


async def test_index_rebuilds_from_authoritative_archive(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="rebuild marker"),)),),
        turn_start=0,
        turn_end=0,
    )
    (tmp_path / "s1" / "index.sqlite3").unlink()
    hits = await archive.search("s1", "rebuild marker")
    assert hits and hits[0].ref == ref


async def test_corrupt_index_is_recreated_from_authoritative_archive(
    tmp_path: Path,
) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="recover index"),)),),
        turn_start=0,
        turn_end=0,
    )
    (tmp_path / "s1" / "index.sqlite3").write_bytes(b"not sqlite")
    hits = await archive.search("s1", "recover index")
    assert hits and hits[0].ref == ref


async def test_read_rejects_tampered_segment_content(tmp_path: Path) -> None:
    archive = FileCompactionArchive(tmp_path)
    ref = await archive.append(
        "s1",
        kind="micro",
        messages=(Message(role="user", content=(TextBlock(text="original"),)),),
        turn_start=0,
        turn_end=0,
    )
    path = tmp_path / "s1" / "segments.jsonl"
    path.write_text(path.read_text().replace("original", "tampered"), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity check failed"):
        await archive.read("s1", ref.segment_id)
