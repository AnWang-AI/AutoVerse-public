"""Unit tests for trace_index pure functions (docs/architecture/runtime-flow.md §4.9)."""

from __future__ import annotations

from autoverse.core.context.trace_index import (
    build_trace_index,
    extract_last_trace_turn_end,
    strip_trace_blocks,
)


# ── build_trace_index ──────────────────────────────────────────────────────


def test_build_trace_index_basic_format() -> None:
    block = build_trace_index("sess-abc", 0, 8, "/tmp/trace.jsonl")
    assert block == "\n[Trace: session=sess-abc turns=0–8]\n→ /tmp/trace.jsonl"


def test_build_trace_index_empty_session_returns_empty() -> None:
    assert build_trace_index("", 0, 5, "/tmp/t.jsonl") == ""


def test_build_trace_index_turn_range_in_output() -> None:
    block = build_trace_index("s1", 4, 12, "/p/t.jsonl")
    assert "turns=4–12" in block


# ── extract_last_trace_turn_end ────────────────────────────────────────────


def test_extract_turn_end_en_dash() -> None:
    text = "[Trace: session=s1 turns=0–8]\n→ /tmp/t.jsonl"
    assert extract_last_trace_turn_end(text) == 8


def test_extract_turn_end_hyphen() -> None:
    text = "[Trace: session=s1 turns=0-3]\n→ /tmp/t.jsonl"
    assert extract_last_trace_turn_end(text) == 3


def test_extract_turn_end_none_when_no_block() -> None:
    assert extract_last_trace_turn_end("no trace here") is None


def test_extract_turn_end_returns_last_block() -> None:
    text = (
        "[Trace: session=s1 turns=0–3]\n→ /a.jsonl\n"
        "some summary text\n"
        "[Trace: session=s1 turns=4–9]\n→ /b.jsonl"
    )
    assert extract_last_trace_turn_end(text) == 9


# ── strip_trace_blocks ─────────────────────────────────────────────────────


def test_strip_single_block() -> None:
    text = "summary here\n[Trace: session=s1 turns=0–3]\n→ /tmp/t.jsonl"
    result = strip_trace_blocks(text)
    assert "[Trace:" not in result
    assert "summary here" in result


def test_strip_multiple_blocks() -> None:
    text = (
        "summary\n[Trace: session=s1 turns=0–3]\n→ /a.jsonl\n"
        "middle\n[Trace: session=s1 turns=4–9]\n→ /b.jsonl"
    )
    result = strip_trace_blocks(text)
    assert "[Trace:" not in result
    assert "summary" in result
    assert "middle" in result


def test_strip_no_blocks_unchanged() -> None:
    text = "just a summary, no trace"
    assert strip_trace_blocks(text) == text.rstrip()


def test_strip_block_at_end() -> None:
    text = "summary text\n[Trace: session=s1 turns=0–5]\n→ /tmp/t.jsonl"
    result = strip_trace_blocks(text)
    assert result == "summary text"
