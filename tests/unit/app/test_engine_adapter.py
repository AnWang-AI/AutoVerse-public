"""Unit tests for EngineAdapter diagnostics (docs/architecture/contracts.md §6.6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from autoverse.app.engine import EngineAdapter, QueueEventSink
from autoverse.types import EventType, StreamEvent


class _BoomLoop:
    async def run_turn(self, _options: object = None) -> None:
        raise RuntimeError("loop exploded")


class _Conversation:
    async def sync_from_store(self) -> None:
        return None

    async def append(self, _message: object) -> None:
        return None


async def test_engine_adapter_emits_error_event_before_reraising() -> None:
    queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
    sink = QueueEventSink(queue)
    app = SimpleNamespace(
        config=SimpleNamespace(max_turns=1),
        events=sink,
        store=None,
        extra={},
    )
    engine = EngineAdapter(
        loop=_BoomLoop(),  # type: ignore[arg-type]
        conversation=_Conversation(),  # type: ignore[arg-type]
        app=app,  # type: ignore[arg-type]
        queue=queue,
        session_id="s-boom",
    )

    events: list[StreamEvent] = []
    with pytest.raises(RuntimeError, match="loop exploded"):
        async for event in engine.run("hello"):
            events.append(event)

    errors = [event for event in events if event.type is EventType.ERROR]
    assert errors
    payload: Any = errors[-1].payload
    assert payload.error_class == "RuntimeError"
    assert payload.phase == "app"
    assert "loop exploded" in payload.message
