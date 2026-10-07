"""Conversation — the authoritative in-memory message log (docs/architecture/runtime-flow.md).

The Conversation owns the canonical message list for a session and keeps it in
lock-step with the durable :class:`~autoverse.ports.Store`. The write contract
is *store-first* (先盘后存): every mutating operation persists through the Store
*before* the in-memory view is updated, so a Store failure leaves the in-memory
view untouched (L11) and the two never silently diverge.

`view()` returns an immutable :class:`ConversationView` snapshot whose
``messages`` always equals ``await store.load(session_id)`` (L9). Rewrites pass
the caller-supplied ``reason`` straight through to ``Store.rewrite`` (L10).

This module lives in ``core`` and imports ``ports``/``types`` plus the pure
``core.context.split`` token-estimation helper (the single source of truth for
context sizing; it itself imports only ``types``, so there is no cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from autoverse.core.context.split import _estimate_messages
from autoverse.ports import Store
from autoverse.types.messages import Message


def _estimate_tokens(messages: tuple[Message, ...]) -> int:
    """Cheap byte/3 heuristic — delegates to the shared estimator so the view
    and the compaction policy always agree on size.

    Per-block counting is aligned with what the wire codec actually sends: a
    tool result's ``metadata`` (which can hold megabytes of base64 image data)
    and an image block's base64 ``data`` are deliberately *not* counted by their
    repr — see :func:`autoverse.core.context.split._estimate_block`.
    """
    return _estimate_messages(messages)


@dataclass(frozen=True)
class ConversationView:
    """Immutable snapshot of a conversation at a point in time."""

    messages: tuple[Message, ...]
    protected_idx: int = 0

    @property
    def token_estimate(self) -> int:
        return _estimate_tokens(self.messages)

    def index_of(self, message: Message) -> int:
        """Return the index of *message* by identity/equality, or -1 if absent."""
        for i, m in enumerate(self.messages):
            if m is message or m == message:
                return i
        return -1


class Conversation:
    """Owns the canonical message log; mirrors it durably via the Store."""

    def __init__(self, store: Store, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._messages: list[Message] = []
        self._protected_idx = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    async def hydrate(self) -> None:
        """Load the persisted log into memory (replaces the in-memory view)."""
        loaded = await self._store.load(self._session_id)
        self._messages = list(loaded)

    def hydrate_from(self, messages: tuple[Message, ...]) -> None:
        """Seed memory from a Store-derived immutable frontend snapshot.

        A read-only frontend can reuse the transcript it already loaded when it
        later activates an engine. Turn start still calls ``sync_from_store``,
        so cross-process writes remain authoritative.
        """
        self._messages = list(messages)

    async def sync_from_store(self) -> None:
        """Re-load the persisted log from the Store, picking up external writes.

        Called at the start of each turn (before appending the user message) so
        a multi-frontend setup (TUI + WebUI as separate processes sharing one
        JSONL store) never sends the model a stale context: if the other
        frontend ran a turn or compacted/cleared, this engine sees the latest
        on-disk state before building the LLM request. Idempotent — a no-op
        when the disk already matches memory (just one ``store.load`` cost)."""
        loaded = await self._store.load(self._session_id)
        self._messages = list(loaded)

    async def append(self, *msgs: Message) -> None:
        """Persist then record (先盘后存).

        If ``Store.append`` raises, the in-memory view is left unchanged (L11).
        """
        if not msgs:
            return
        await self._store.append(self._session_id, *msgs)
        self._messages.extend(msgs)

    async def extend(self, msgs: list[Message]) -> None:
        """Alias for :meth:`append` that takes a list (P-compat)."""
        await self.append(*msgs)

    async def rewrite(self, msgs: list[Message], reason: str) -> None:
        """Replace the whole log, passing *reason* through to the Store (L10).

        Store-first: on Store failure the in-memory view is left unchanged.
        """
        await self._store.rewrite(self._session_id, list(msgs), reason)
        self._messages = list(msgs)

    def set_protected_idx(self, idx: int) -> None:
        self._protected_idx = max(0, idx)

    @property
    def protected_idx(self) -> int:
        return self._protected_idx

    def __len__(self) -> int:
        return len(self._messages)

    def view(self) -> ConversationView:
        """Immutable snapshot; ``messages`` mirrors the durable log (L9)."""
        return ConversationView(
            messages=tuple(self._messages),
            protected_idx=self._protected_idx,
        )


__all__ = ["Conversation", "ConversationView"]
