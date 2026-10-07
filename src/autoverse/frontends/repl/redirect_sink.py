"""RedirectSink — a re-targetable EventSink seam for background sessions.

docs/architecture/frontend-adapters.md. The engine is built once with a stable
downstream sink baked into its ``QueueEventSink``; but a session's events should
only paint the single shared ``#log`` while that session is **foreground**. When
the user switches away, the running turn keeps going (detached) yet must stop
writing to the now-reused ``#log``.

``RedirectSink`` is that seam: every engine is wired to one of these instead of a
concrete :class:`~autoverse.frontends.repl.tui_sink.TuiEventSink`. Its ``target``
is the live foreground sink while the session is current, and ``None`` while it
is backgrounded — so detaching is a single assignment, not a teardown. An
optional ``observer`` is always notified (foreground *and* background) so the app
can track attention/terminal events for the rail regardless of where the turn is
rendered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.ports import EventSink
    from autoverse.types import StreamEvent


class RedirectSink:
    """An :class:`~autoverse.ports.EventSink` that forwards to a swappable target.

    * ``target`` set   → forward each event to it (foreground → paints ``#log``).
    * ``target`` None  → drop the render (backgrounded), but still observe.

    Conversation persistence is unaffected: the engine appends messages to its
    own store independently of the sink, so a detached turn still lands on disk.
    """

    def __init__(
        self,
        target: "EventSink | None" = None,
        *,
        observer: "Callable[[StreamEvent], None] | None" = None,
    ) -> None:
        self.target = target
        self._observer = observer

    def set_target(self, target: "EventSink | None") -> None:
        self.target = target

    async def emit(self, event: "StreamEvent") -> None:
        if self._observer is not None:
            try:
                self._observer(event)
            except Exception:  # noqa: BLE001 — observation must never break the stream
                pass
        target = self.target
        if target is not None:
            await target.emit(event)


__all__ = ["RedirectSink"]
