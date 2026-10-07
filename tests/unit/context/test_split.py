"""Unit tests for pure compaction split helpers."""

from __future__ import annotations

from autoverse.core.context.split import (
    _estimate_block,
    _estimate_messages,
    adjust_tail_boundary,
    estimate_summary_prompt,
    extract_original_user_goal,
    extract_prior_summary,
    merge_summaries,
    safe_split_idx,
    split_for_micro_compact,
    truncate_to_fit,
)
from autoverse.types.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=(TextBlock(text=text),))  # type: ignore[arg-type]


def _use(role: str, tid: str) -> Message:
    return Message(role=role, content=(ToolUseBlock(id=tid, name="f", input={}),))  # type: ignore[arg-type]


def _result(role: str, tid: str) -> Message:
    return Message(role=role, content=(ToolResultBlock(tool_use_id=tid, content="ok"),))  # type: ignore[arg-type]


def test_estimate_messages_byte_over_3() -> None:
    msgs = (_text("user", "x" * 30),)
    assert _estimate_messages(msgs) == 10


def test_estimate_tool_result_ignores_base64_metadata() -> None:
    # generate_image (docs/reference/tools.md §11.7) stashes a (potentially multi-MB) base64
    # image in metadata; the codec never sends it, so the estimate must count
    # only ``content`` and never blow past the truncate ceiling.
    huge_b64 = "A" * 4_000_000  # ~4MB base64 — a typical generated image
    block = ToolResultBlock(
        tool_use_id="t1",
        content="Generated image: out.png",
        metadata={"images": [huge_b64], "image_media_type": "image/png"},
    )
    assert _estimate_block(block) == len("Generated image: out.png")


def test_estimate_image_block_is_flat_nominal() -> None:
    # An image block costs a fixed nominal, not its base64 ``data`` length.
    small = ImageBlock(media_type="image/png", data="A" * 10)
    large = ImageBlock(media_type="image/png", data="A" * 4_000_000)
    assert _estimate_block(small) == _estimate_block(large)
    assert _estimate_block(large) < 10_000  # bounded, not proportional to data


def test_estimate_messages_with_image_metadata_stays_small() -> None:
    # A whole message carrying the heavy tool result must not explode the
    # message-level estimate (the bug that caused the "Context truncated" loop).
    huge_b64 = "A" * 4_000_000
    msg = Message(
        role="user",
        content=(
            ToolResultBlock(
                tool_use_id="t1",
                content="Generated image: out.png",
                metadata={"images": [huge_b64]},
            ),
        ),
    )
    assert _estimate_messages((msg,)) < 1_000


def test_L12_adjust_tail_boundary_does_not_orphan_result() -> None:
    # head=[use t1], tail starts with the matching result -> must advance past it.
    msgs = (_use("assistant", "t1"), _result("user", "t1"), _text("assistant", "done"))
    idx = adjust_tail_boundary(msgs, 1)
    assert idx == 2  # advanced past the result so it stays with its use


def test_L12_safe_split_keeps_tail_and_pairs() -> None:
    msgs = (
        _text("user", "goal"),
        _use("assistant", "t1"),
        _result("user", "t1"),
        _text("assistant", "more"),
    )
    idx = safe_split_idx(msgs, keep_tail=2)
    # never cut between use(idx1) and result(idx2)
    assert idx in (0, 1, 3)
    head = msgs[:idx]
    head_ids = {b.id for m in head for b in m.content if isinstance(b, ToolUseBlock)}
    tail = msgs[idx:]
    for m in tail:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                assert b.tool_use_id not in head_ids


def test_safe_split_returns_zero_when_short() -> None:
    msgs = (_text("user", "a"), _text("assistant", "b"))
    assert safe_split_idx(msgs, keep_tail=4) == 0


def test_split_for_micro_compact_folds_only_noise() -> None:
    msgs = (
        _use("assistant", "t1"),
        _result("user", "t1"),
        _text("assistant", "kept"),
        _text("user", "tail1"),
        _text("user", "tail2"),
    )
    foldable, rest = split_for_micro_compact(msgs, keep_tail=2)
    assert all(
        all(isinstance(b, (ToolUseBlock, ToolResultBlock)) for b in m.content)
        for m in foldable
    )
    assert _text("assistant", "kept") in rest


def test_L13_extract_prior_summary_and_goal() -> None:
    summary = Message(
        role="system",
        content=(TextBlock(text="[CONVERSATION SUMMARY]\nstuff"),),  # type: ignore[arg-type]
    )
    msgs = (_text("user", "the goal"), summary, _text("assistant", "ok"))
    assert "stuff" in extract_prior_summary(msgs)
    assert extract_original_user_goal(msgs) == "the goal"


def test_L13_merge_summaries_returns_one_replacement() -> None:
    merged = merge_summaries("[CONVERSATION SUMMARY]\nold", "new content")
    assert merged.startswith("[CONVERSATION SUMMARY]")
    assert "new content" in merged
    assert "old" not in merged
    assert "SUPERSEDED" not in merged


def test_merge_summaries_empty_prior() -> None:
    merged = merge_summaries("", "fresh")
    assert merged.startswith("[CONVERSATION SUMMARY]")
    assert "fresh" in merged
    assert "SUPERSEDED" not in merged


def test_truncate_to_fit() -> None:
    out = truncate_to_fit("y" * 100, 40)
    assert len(out) <= 40
    assert out.endswith("[truncated]…")
    assert truncate_to_fit("short", 40) == "short"


def test_estimate_summary_prompt_includes_template() -> None:
    msgs = (_text("user", "x" * 30),)
    est = estimate_summary_prompt(msgs, "t" * 30)
    assert est == 10 + 10
