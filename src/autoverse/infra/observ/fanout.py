"""FanOutSink — broadcast one emit to N child sinks (docs/reference/events-and-storage.md §3.2, O8).

core holds a single ``EventSink``; the assembly layer fans renderer + trace +
OTel into one ``FanOutSink``. One sink raising must not break the others: each
emit is isolated, the error swallowed and recorded so the rest still receive the
event. ``aclose`` (not part of the EventSink Port) forwards to children that
implement it.
"""

from __future__ import annotations

import logging
from typing import Sequence

from autoverse.ports import EventSink
from autoverse.types.events import StreamEvent

_LOG = logging.getLogger(__name__)


class FanOutSink:
    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self._sinks: tuple[EventSink, ...] = tuple(sinks)
        #: (sink_index, exception) for each swallowed emit failure.
        self.errors: list[tuple[int, BaseException]] = []
        #: JSON-safe diagnostic summaries for swallowed sink failures.
        self.diagnostics: list[dict[str, object]] = []

    @property
    def sinks(self) -> tuple[EventSink, ...]:
        return self._sinks

    async def emit(self, event: StreamEvent) -> None:
        for i, sink in enumerate(self._sinks):
            try:
                await sink.emit(event)
            except Exception as exc:  # noqa: BLE001 — isolate one bad sink
                self.errors.append((i, exc))
                _LOG.exception(
                    "event sink %s failed while handling %s", i, event.type.value
                )
                self.diagnostics.append(
                    {
                        "sink_index": i,
                        "event_type": event.type.value,
                        "error_class": type(exc).__name__,
                        "message": str(exc) or type(exc).__name__,
                    }
                )

    async def aclose(self, reason: str = "normal") -> None:
        for sink in self._sinks:
            close = getattr(sink, "aclose", None)
            if close is None:
                continue
            try:
                await close(reason)
            except Exception:  # noqa: BLE001
                pass


__all__ = ["FanOutSink"]
