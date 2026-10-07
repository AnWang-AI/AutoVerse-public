"""Unit tests for stream accumulation."""

from __future__ import annotations

from typing import AsyncIterator, Any

from autoverse.core.loop.accumulator import collect_assistant
from autoverse.types import (
    StreamEvent,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolStartPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.types.messages import TextBlock, ThinkingBlock, ToolUseBlock


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


async def _gen(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    for e in events:
        yield e


async def test_collect_text_and_usage_and_stop_reason() -> None:
    events = [
        make_event(TextDeltaPayload(text="Hel")),
        make_event(TextDeltaPayload(text="lo")),
        make_event(UsagePayload(usage=Usage(input_tokens=5, output_tokens=2), model="m")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    sink = _Sink()
    out = await collect_assistant(_gen(events), sink)
    assert isinstance(out.message.content[0], TextBlock)
    assert out.message.content[0].text == "Hello"
    assert out.usage.total == 7
    assert out.stop_reason == "END_TURN"
    assert out.message.metadata["stop_reason"] == "END_TURN"
    assert out.message.metadata["usage_model"] == "m"
    assert out.message.metadata["usage"] == {
        "input_tokens": 5,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    # Provider TURN_END terminates this LLM iteration internally.  The TurnLoop
    # emits the one public TURN_END for the complete user turn.
    assert sink.events == events[:-1]


async def test_collect_tool_calls() -> None:
    events = [
        make_event(ToolStartPayload(tool_use_id="t1", tool_name="bash", tool_input={"c": "ls"})),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
    ]
    sink = _Sink()
    out = await collect_assistant(_gen(events), sink)
    assert len(out.tool_calls) == 1
    assert isinstance(out.tool_calls[0], ToolUseBlock)
    assert out.tool_calls[0].name == "bash"
    # The dispatcher emits the public execution TOOL_START after permission;
    # the provider control event must not create a duplicate frontend card.
    assert sink.events == []


async def test_collect_thinking_block() -> None:
    events = [
        make_event(ThinkingDeltaPayload(text="pondering")),
        make_event(TextDeltaPayload(text="answer")),
    ]
    out = await collect_assistant(_gen(events), _Sink())
    assert isinstance(out.message.content[0], ThinkingBlock)
    assert isinstance(out.message.content[1], TextBlock)


async def test_collect_empty_response_yields_empty_text() -> None:
    out = await collect_assistant(_gen([]), _Sink())
    assert len(out.message.content) == 1
    assert isinstance(out.message.content[0], TextBlock)
    assert out.message.content[0].text == ""


async def test_max_tokens_stop_reason_recorded() -> None:
    events = [
        make_event(TextDeltaPayload(text="partial")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="MAX_TOKENS")),
    ]
    out = await collect_assistant(_gen(events), _Sink())
    assert out.stop_reason == "MAX_TOKENS"
    assert out.message.metadata["stop_reason"] == "MAX_TOKENS"
