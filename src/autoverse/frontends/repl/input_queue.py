"""MessageQueue + explicit InputMode stack — concurrent input model (R5, D4/D7).

While the engine is running the user may keep typing; submissions are enqueued
in the :class:`MessageQueue` (priority: ``now`` > ``next`` > ``later``) and
dequeued once the engine is idle. The current input arbitration mode is held in
an explicit :class:`InputModeStack` (enum-based) rather than scattered boolean
flags (D7); the stack pops on exception via ``finally`` so a modal that crashes
never leaves the REPL stuck.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from enum import Enum
from typing import Iterator, Literal


Priority = Literal["now", "next", "later"]
_ORDER: dict[Priority, int] = {"now": 0, "next": 1, "later": 2}


class MessageQueue:
    """Priority FIFO of pending user inputs (thread-agnostic, asyncio-safe usage)."""

    def __init__(self) -> None:
        # One deque per priority bucket preserves FIFO within a priority. Each
        # entry is ``(text, mode)`` where ``mode`` is the interaction mode active
        # when the message was enqueued (normal/plan/worktree) — surfaced in the
        # queued panel so a pending message carries the mode it was sent under,
        # independent of later Tab mode changes (docs/guides/using-cli-and-tui.md §3.10).
        self._buckets: dict[Priority, deque[tuple[str, str]]] = {
            "now": deque(),
            "next": deque(),
            "later": deque(),
        }

    def enqueue(self, text: str, priority: Priority = "next", *, mode: str = "normal") -> None:
        self._buckets[priority].append((text, mode))

    def dequeue(self) -> str | None:
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            bucket = self._buckets[prio]
            if bucket:
                return bucket.popleft()[0]
        return None

    def peek(self) -> str | None:
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            bucket = self._buckets[prio]
            if bucket:
                return bucket[0][0]
        return None

    def items(self) -> list[tuple[Priority, str]]:
        """Read-only snapshot of pending inputs in dequeue order.

        Returns ``(priority, text)`` pairs ordered ``now > next > later`` with
        FIFO preserved within each priority — the same order :meth:`dequeue`
        would yield them. See :meth:`items_with_mode` for the variant that also
        carries each message's enqueue-time interaction mode.
        """
        out: list[tuple[Priority, str]] = []
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            for text, _mode in self._buckets[prio]:
                out.append((prio, text))
        return out

    def items_with_mode(self) -> list[tuple[Priority, str, str]]:
        """Like :meth:`items` but each tuple is ``(priority, text, mode)``.

        ``mode`` is the interaction mode (normal/plan/worktree) active when the
        message was enqueued. Used by the TUI to tag each queued row with the
        mode it was sent under.
        """
        out: list[tuple[Priority, str, str]] = []
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            for text, mode in self._buckets[prio]:
                out.append((prio, text, mode))
        return out

    def pop_index(self, index: int) -> str | None:
        """Remove and return the item at *index* in :meth:`items` order.

        ``index`` is the flat position across all priority buckets (the same
        ordering the TUI panel renders). Out-of-range indices return ``None``.
        Used to cancel a specific queued message the user picked from the panel.
        """
        if index < 0:
            return None
        seen = 0
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            bucket = self._buckets[prio]
            if index < seen + len(bucket):
                local = index - seen
                text = bucket[local][0]
                del bucket[local]
                return text
            seen += len(bucket)
        return None

    def pop_last(self) -> str | None:
        """Remove and return the most recently enqueued item (last in :meth:`items`).

        The flat :meth:`items` order is ``now > next > later`` with FIFO within a
        bucket, so the *last* pending submission is the rightmost item of the
        highest-``_ORDER`` non-empty bucket. Used by the TUI "edit last queued"
        gesture (Ctrl+E) to pull a pending message back into the input box for
        editing instead of cancelling and retyping it. Returns ``None`` when the
        queue is empty.
        """
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p], reverse=True):
            bucket = self._buckets[prio]
            if bucket:
                return bucket.pop()[0]
        return None

    def remove_first(self, text: str) -> bool:
        """Remove the first item whose text equals *text* (in :meth:`items` order).

        Defensive fallback for cancellation when a flat index may have drifted
        (e.g. a turn boundary drained an item between snapshot and removal).
        Returns ``True`` if an item was removed.
        """
        for prio in sorted(self._buckets, key=lambda p: _ORDER[p]):
            bucket = self._buckets[prio]
            for i, (candidate, _mode) in enumerate(bucket):
                if candidate == text:
                    del bucket[i]
                    return True
        return False

    def clear(self) -> int:
        n = len(self)
        for bucket in self._buckets.values():
            bucket.clear()
        return n

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets.values())

    def __bool__(self) -> bool:
        return len(self) > 0


class InputMode(Enum):
    """Explicit input arbitration modes (D7) — no scattered booleans."""

    NORMAL = "normal"        # idle: input executes / enqueues immediately
    RUNNING = "running"      # engine busy: input is queued
    LEADER = "leader"        # leader-key sequence pending
    PERMISSION = "permission"  # awaiting an approval decision
    PALETTE = "palette"      # command palette open
    MULTICHOICE = "multichoice"  # multi-select dialog open


class InputModeStack:
    """A small stack of :class:`InputMode`; the top decides input behaviour."""

    def __init__(self) -> None:
        self._stack: list[InputMode] = [InputMode.NORMAL]

    def current(self) -> InputMode:
        return self._stack[-1]

    def depth(self) -> int:
        return len(self._stack)

    def has(self, mode: InputMode) -> bool:
        """True when *mode* is present anywhere in the stack (not just on top).

        Background sessions (docs/architecture/frontend-adapters.md §5.6.3) keep a turn alive while a
        choice/permission menu may be pushed on top, so callers ask "is the
        foreground turn running" with ``has(RUNNING)`` rather than ``current()``.
        """
        return mode in self._stack

    def enter(self, mode: InputMode) -> None:
        """Idempotently push *mode* (non-context form of :meth:`push`).

        Used for the foreground RUNNING band, whose lifetime no longer maps to a
        single ``with`` block: switching into a still-running background session
        turns it on; the turn finishing (or switching away) turns it off."""
        if mode not in self._stack:
            self._stack.append(mode)

    def leave(self, mode: InputMode) -> None:
        """Remove the most recent occurrence of *mode*, if present (no-op else)."""
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i] is mode:
                del self._stack[i]
                break

    @contextmanager
    def push(self, mode: InputMode) -> Iterator[None]:
        self._stack.append(mode)
        try:
            yield
        finally:
            # Remove the most recent occurrence of *mode* — robust to nesting.
            for i in range(len(self._stack) - 1, 0, -1):
                if self._stack[i] is mode:
                    del self._stack[i]
                    break


__all__ = [
    "MessageQueue",
    "Priority",
    "InputMode",
    "InputModeStack",
]
