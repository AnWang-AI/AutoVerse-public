"""Acceptance L9-L11: Conversation store-mirroring invariants."""

from __future__ import annotations

import pytest

from autoverse.core.state.conversation import Conversation
from autoverse.types.messages import Message, TextBlock

from tests._doubles import MemStore, RaisingStore


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=(TextBlock(text=text),))  # type: ignore[arg-type]


async def test_L9_view_messages_equal_store_load() -> None:
    """L9: view().messages == await store.load(session_id)."""
    store = MemStore()
    conv = Conversation(store, "sid")
    await conv.append(_msg("user", "hi"), _msg("assistant", "yo"))
    view = conv.view()
    assert list(view.messages) == await store.load("sid")


async def test_L10_rewrite_reason_passes_through() -> None:
    """L10: rewrite reason is forwarded verbatim to Store.rewrite."""
    store = MemStore()
    conv = Conversation(store, "sid")
    await conv.append(_msg("user", "a"), _msg("assistant", "b"))
    await conv.rewrite([_msg("system", "summary")], reason="meta_compact")
    assert store.rewrite_log[-1] == ("sid", 1, "meta_compact")


async def test_L11_store_first_failure_leaves_view_unchanged() -> None:
    """L11: when Store.append raises, the in-memory view is not updated."""
    store = RaisingStore()
    conv = Conversation(store, "sid")
    with pytest.raises(RuntimeError):
        await conv.append(_msg("user", "boom"))
    assert conv.view().messages == ()
    assert list(conv.view().messages) == await store.load("sid")
