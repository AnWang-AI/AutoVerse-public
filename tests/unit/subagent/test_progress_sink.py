"""_SubagentProgressSink: heartbeat on turn, suppress-but-capture child cards.

Locks the contract that a sub-agent's raw events stay OUT of the parent
scrollback yet are forwarded as SUBAGENT_CHILD for drill-down, and that each
child TURN_END becomes a parent SUBAGENT_ACTIVITY running heartbeat (§6.4).
"""

from __future__ import annotations

from typing import Any

from autoverse.app.subagent import _SubagentProgressSink
from autoverse.types import make_event
from autoverse.types.events import (
    EventType,
    SubagentChildPayload,
    TextDeltaPayload,
    ToolEndPayload,
    TurnEndPayload,
)


class _Parent:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


async def test_child_card_forwarded_as_subagent_child_not_raw() -> None:
    parent = _Parent()
    sink = _SubagentProgressSink(parent, job_id="sa-1", subagent_type="scout")

    await sink.emit(make_event(TextDeltaPayload(text="hello")))
    await sink.emit(make_event(ToolEndPayload("t", "grep", "a.py")))

    # Parent never sees the raw TEXT_DELTA / TOOL_END — only SUBAGENT_CHILD wraps.
    assert all(e.type is EventType.SUBAGENT_CHILD for e in parent.events)
    assert len(parent.events) == 2
    first = parent.events[0].payload
    assert isinstance(first, SubagentChildPayload)
    assert first.job_id == "sa-1" and first.subagent_type == "scout"
    assert first.inner.type is EventType.TEXT_DELTA
    assert first.inner.payload.text == "hello"


async def test_turn_end_becomes_running_heartbeat() -> None:
    parent = _Parent()
    sink = _SubagentProgressSink(parent, job_id="sa-2", subagent_type="builder")

    await sink.emit(make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")))
    await sink.emit(make_event(TurnEndPayload(turn_index=1, iterations=1, stop_cause="end_turn")))

    acts = [e for e in parent.events if e.type is EventType.SUBAGENT_ACTIVITY]
    assert len(acts) == 2
    assert [a.payload.turn for a in acts] == [1, 2]              # incrementing
    assert all(a.payload.status == "running" for a in acts)
    assert all(a.payload.job_id == "sa-2" for a in acts)
    # TURN_END is consumed as a heartbeat — never forwarded as SUBAGENT_CHILD.
    assert all(e.type is not EventType.SUBAGENT_CHILD for e in parent.events)
