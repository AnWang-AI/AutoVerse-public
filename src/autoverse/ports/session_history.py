"""Read-only semantic history windows for transcript-oriented frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from autoverse.types import Message


@dataclass(frozen=True)
class SessionHistoryWindow:
    """A complete-turn message window and opaque cursor to older content."""

    messages: tuple[Message, ...]
    before_cursor: str | None = None


@runtime_checkable
class SessionHistoryPort(Protocol):
    """Load bounded, turn-aligned transcript windows without engine activation."""

    async def load_tail(
        self, session_id: str, *, minimum_turns: int = 2
    ) -> SessionHistoryWindow:
        """Return the newest complete semantic turns."""
        ...

    async def load_before(
        self, session_id: str, before_cursor: str
    ) -> SessionHistoryWindow:
        """Return the complete turn immediately before an opaque cursor."""
        ...


__all__ = ["SessionHistoryPort", "SessionHistoryWindow"]
