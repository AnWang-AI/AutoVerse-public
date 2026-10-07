"""InterruptHandler — two-stage Ctrl+C (interrupt → cancel) (R6).

While the engine is running, the first Ctrl+C requests ``engine.abort("interrupt")``
(soft: tools may background to preserve partial output); a second Ctrl+C requests
``engine.abort("cancel")`` (hard: terminate immediately). The press counter is
reset on each new turn so the next interrupt starts again at the soft stage.

The handler only signals the engine via the narrow :class:`EngineHandle` facade;
it never holds an AbortController or kills tasks itself — reversibility lives in
core (P3).
"""

from __future__ import annotations

from typing import Protocol

from autoverse.types import AbortReason


class _Abortable(Protocol):
    def abort(self, reason: AbortReason = "cancel") -> None: ...


class InterruptHandler:
    """Track Ctrl+C presses within a turn and escalate the abort reason."""

    def __init__(self, engine: _Abortable) -> None:
        self._engine = engine
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def reset(self) -> None:
        """Reset the press counter — call at the start of every new turn."""
        self._count = 0

    def on_interrupt(self) -> AbortReason:
        """Handle one Ctrl+C while running; return the abort reason signalled."""
        self._count += 1
        reason: AbortReason = "interrupt" if self._count == 1 else "cancel"
        self._engine.abort(reason)
        return reason


__all__ = ["InterruptHandler"]
