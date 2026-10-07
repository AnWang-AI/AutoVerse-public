"""Throttle / merge helper for TEXT_DELTA streaming (docs/reference/extensions.md).

Platforms have outbound rate limits; ``TEXT_DELTA`` may arrive dozens of times a
second. ``DeltaThrottle`` accumulates deltas and only releases a merged chunk
when a window boundary is crossed — by **time** (default 1s) or by **character
count** (default 200 chars), whichever comes first. ``TURN_END`` forces a final
flush so nothing is left buffered.

The helper is transport-agnostic and clock-injectable (``now`` callable) so the
merge logic is unit-testable without real time (P4).
"""

from __future__ import annotations

from typing import Callable


class DeltaThrottle:
    """Accumulate text deltas; emit merged chunks at window boundaries.

    Non-streaming platforms merge everything into one message (flushed at
    ``TURN_END``); streaming platforms flush at each window to edit-in-place.
    """

    def __init__(
        self,
        *,
        time_window_s: float = 1.0,
        char_window: int = 200,
        merge_all: bool = False,
        now: Callable[[], float] | None = None,
    ) -> None:
        """
        :param merge_all: when True (non-streaming platforms) never flush on a
            window — accumulate until ``flush_final`` so the whole turn becomes
            a single merged message (G2).
        """
        self._time_window_s = time_window_s
        self._char_window = char_window
        self._merge_all = merge_all
        self._now = now or _monotonic
        self._buf: list[str] = []
        self._chars = 0
        self._last_flush: float | None = None

    def add(self, text: str) -> str | None:
        """Add a delta. Return a merged chunk if a window boundary is crossed,
        else ``None``. For ``merge_all`` platforms always returns ``None`` (the
        chunk is released only by :meth:`flush_final`)."""
        if not text:
            return None
        self._buf.append(text)
        self._chars += len(text)
        now = self._now()
        if self._last_flush is None:
            self._last_flush = now
        if self._merge_all:
            return None
        crossed_chars = self._chars >= self._char_window
        crossed_time = (now - self._last_flush) >= self._time_window_s
        if crossed_chars or crossed_time:
            return self._drain(now)
        return None

    def pending(self) -> str:
        """Buffered (un-flushed) text without draining."""
        return "".join(self._buf)

    def flush_final(self) -> str | None:
        """Drain everything buffered (call at TURN_END). ``None`` if empty."""
        if not self._buf:
            return None
        return self._drain(self._now())

    def _drain(self, now: float) -> str:
        chunk = "".join(self._buf)
        self._buf.clear()
        self._chars = 0
        self._last_flush = now
        return chunk


def _monotonic() -> float:
    import time

    return time.monotonic()


__all__ = ["DeltaThrottle"]
