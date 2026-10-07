"""Cancellation propagation (docs/architecture/contracts.md §2).

Two-level semantics threaded through the LLM stream and tool execution:
  - "interrupt": user interrupt; tools should background to preserve output.
  - "cancel":    hard cancel; tools should terminate immediately.
"""

from __future__ import annotations

import asyncio
from typing import Literal

AbortReason = Literal["interrupt", "cancel"]


class AbortError(Exception):
    """Raised by tools/LLM when aborted; dispatch layer turns it into feedback."""

    def __init__(self, reason: AbortReason = "cancel") -> None:
        super().__init__(f"aborted: {reason}")
        self.reason: AbortReason = reason


class AbortSignal:
    """Read-only view of an AbortController."""

    def __init__(self, controller: "AbortController") -> None:
        self._controller = controller

    @property
    def aborted(self) -> bool:
        return self._controller._aborted

    @property
    def reason(self) -> AbortReason:
        return self._controller._reason

    async def wait(self) -> AbortReason:
        await self._controller._event.wait()
        return self._controller._reason

    def raise_if_aborted(self) -> None:
        if self._controller._aborted:
            raise AbortError(self._controller._reason)


class AbortController:
    def __init__(self) -> None:
        self._aborted = False
        self._reason: AbortReason = "cancel"
        self._event = asyncio.Event()
        self._signal = AbortSignal(self)

    def abort(self, reason: AbortReason = "cancel") -> None:
        # Idempotent: the first reason is never overwritten.
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        self._event.set()

    @property
    def signal(self) -> AbortSignal:
        return self._signal


__all__ = ["AbortReason", "AbortError", "AbortSignal", "AbortController"]
