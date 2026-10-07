"""Unit tests for loop repair helpers."""

from __future__ import annotations

from autoverse.core.loop.repair import (
    merge_trailing_user,
    repair_orphaned_tool_use,
)
from autoverse.types.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _user(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


def _use(tid: str) -> Message:
    return Message(role="assistant", content=(ToolUseBlock(id=tid, name="f", input={}),))


def _result(tid: str) -> Message:
    return Message(role="user", content=(ToolResultBlock(tool_use_id=tid, content="ok"),))


def test_L7_orphaned_tool_use_gets_placeholder() -> None:
    msgs = [_user("go"), _use("t1")]
    out = repair_orphaned_tool_use(msgs)
    assert len(out) == 3
    last = out[-1]
    assert last.role == "user"
    block = last.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "t1"
    assert block.is_error


def test_L7_answered_tool_use_untouched() -> None:
    msgs = [_user("go"), _use("t1"), _result("t1")]
    out = repair_orphaned_tool_use(msgs)
    assert out == msgs


def test_L7_interrupted_tool_use_repaired_before_new_prompt() -> None:
    """An interrupted tool_use followed by a fresh user prompt: the placeholder
    tool_result must be inserted right after the assistant tool_use, NOT at the
    tail (regression for HTTP 400 'insufficient tool messages following
    tool_calls message')."""
    msgs = [_user("go"), _use("t1"), _user("new prompt after Ctrl+C")]
    out = repair_orphaned_tool_use(msgs)
    assert len(out) == 4
    # placeholder result sits immediately after the tool_use at index 1
    repaired = out[2]
    assert repaired.role == "user"
    block = repaired.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "t1"
    assert block.is_error
    # the user's new prompt remains last, after the repair
    assert out[3].content[0].text == "new prompt after Ctrl+C"  # type: ignore[union-attr]


def test_L7_multiple_orphans_in_one_assistant_message() -> None:
    """Two tool_use blocks in one assistant message, both interrupted: a single
    placeholder user message answers both, inserted directly after."""
    multi = Message(
        role="assistant",
        content=(
            ToolUseBlock(id="t1", name="f", input={}),
            ToolUseBlock(id="t2", name="g", input={}),
        ),
    )
    msgs = [_user("go"), multi, _user("next")]
    out = repair_orphaned_tool_use(msgs)
    assert len(out) == 4
    repaired = out[2]
    ids = [b.tool_use_id for b in repaired.content if isinstance(b, ToolResultBlock)]
    assert ids == ["t1", "t2"]


def test_L8_merge_consecutive_user() -> None:
    msgs = [_user("a"), _user("b"), _user("c")]
    out = merge_trailing_user(msgs)
    assert len(out) == 1
    assert len(out[0].content) == 3


def test_L8_no_merge_when_interleaved() -> None:
    asst = Message(role="assistant", content=(TextBlock(text="hi"),))
    msgs = [_user("a"), asst, _user("b")]
    out = merge_trailing_user(msgs)
    assert len(out) == 3


def test_merge_single_message_passthrough() -> None:
    assert merge_trailing_user([_user("only")]) == [_user("only")]
