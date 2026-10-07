"""Unit tests for Conversation and ConversationView."""

from __future__ import annotations

import pytest

from autoverse.core.state.conversation import Conversation, ConversationView
from autoverse.ports import SessionMeta
from autoverse.types.messages import Message, TextBlock

from tests._doubles import MemStore, RaisingStore


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=(TextBlock(text=text),))  # type: ignore[arg-type]


async def test_append_persists_then_records() -> None:
    store = MemStore()
    conv = Conversation(store, "s1")
    await conv.append(_msg("user", "hi"))
    assert len(conv) == 1
    assert await store.load("s1") == [_msg("user", "hi")]


async def test_extend_alias() -> None:
    store = MemStore()
    conv = Conversation(store, "s1")
    await conv.extend([_msg("user", "a"), _msg("assistant", "b")])
    assert len(conv) == 2


async def test_view_messages_equal_store_load() -> None:
    store = MemStore()
    conv = Conversation(store, "s1")
    await conv.append(_msg("user", "x"), _msg("assistant", "y"))
    view = conv.view()
    assert list(view.messages) == await store.load("s1")


async def test_store_first_on_append_failure() -> None:
    store = RaisingStore()
    conv = Conversation(store, "s1")
    with pytest.raises(RuntimeError):
        await conv.append(_msg("user", "boom"))
    assert len(conv) == 0
    assert conv.view().messages == ()


async def test_rewrite_passes_reason_through() -> None:
    store = MemStore()
    conv = Conversation(store, "s1")
    await conv.append(_msg("user", "a"))
    await conv.rewrite([_msg("system", "compacted")], reason="micro_compact")
    assert store.rewrite_log == [("s1", 1, "micro_compact")]
    assert len(conv) == 1


async def test_hydrate_loads_persisted() -> None:
    store = MemStore()
    await store.append("s1", _msg("user", "old"))
    conv = Conversation(store, "s1")
    await conv.hydrate()
    assert len(conv) == 1


def test_view_token_estimate_and_index_of() -> None:
    m1 = _msg("user", "hello world")
    m2 = _msg("assistant", "reply")
    view = ConversationView(messages=(m1, m2), protected_idx=1)
    assert view.token_estimate > 0
    assert view.index_of(m2) == 1
    assert view.index_of(_msg("user", "absent")) == -1
    assert view.protected_idx == 1


async def test_sync_from_store_picks_up_external_writes() -> None:
    """Two Conversations sharing one store: A appends, B syncs, B sees A's msgs.

    This is the multi-frontend (TUI + WebUI) scenario: each process has its own
    in-memory Conversation, but they share the same JSONL store. Before a turn
    starts, ``sync_from_store`` re-reads the disk so the engine never sends the
    model a stale context."""
    store = MemStore()
    await store.create("shared", SessionMeta(session_id="shared"))

    conv_a = Conversation(store, "shared")
    conv_b = Conversation(store, "shared")
    await conv_a.hydrate()
    await conv_b.hydrate()

    # A runs a turn (appends user + assistant)
    await conv_a.append(_msg("user", "from-tui"))
    await conv_a.append(_msg("assistant", "reply"))

    # B has not synced yet — still sees the old (empty) state
    assert len(conv_b) == 0

    # B syncs before its own turn — now it sees A's messages
    await conv_b.sync_from_store()
    assert len(conv_b) == 2
    texts = [b.text for m in conv_b.view().messages for b in m.content if isinstance(b, TextBlock)]
    assert "from-tui" in texts
    assert "reply" in texts
