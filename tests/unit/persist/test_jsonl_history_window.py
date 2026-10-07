"""Semantic reverse-window reads for the default JSONL Store."""

from __future__ import annotations

from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.ports import SessionMeta
from autoverse.types import Message, TextBlock, ToolResultBlock


async def test_tail_and_before_windows_follow_user_prompt_boundaries(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    sid = "history-window"
    await store.create(sid, SessionMeta(session_id=sid))
    for turn in range(4):
        await store.append(
            sid,
            Message(role="user", content=(TextBlock(f"prompt-{turn}"),)),
            Message(role="assistant", content=(TextBlock(f"answer-{turn}"),)),
            Message(
                role="user",
                content=(ToolResultBlock(f"tool-{turn}", f"result-{turn}"),),
            ),
        )

    tail = await store.load_tail(sid, minimum_turns=2)

    assert [
        block.text
        for message in tail.messages
        for block in message.content
        if isinstance(block, TextBlock) and message.role == "user"
    ] == ["prompt-2", "prompt-3"]
    assert tail.before_cursor is not None

    older = await store.load_before(sid, tail.before_cursor)
    assert [
        block.text
        for message in older.messages
        for block in message.content
        if isinstance(block, TextBlock) and message.role == "user"
    ] == ["prompt-1"]
    assert older.before_cursor is not None

    oldest = await store.load_before(sid, older.before_cursor)
    assert [
        block.text
        for message in oldest.messages
        for block in message.content
        if isinstance(block, TextBlock) and message.role == "user"
    ] == ["prompt-0"]
    assert oldest.before_cursor is None
