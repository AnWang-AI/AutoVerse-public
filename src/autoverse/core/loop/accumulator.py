"""Stream accumulation — fold an LLM event stream into an assistant Message.

``collect_assistant`` consumes a provider stream, forwarding every event to the
EventSink (the loop never re-emits), and accumulates content blocks:

  TEXT_DELTA      -> appended into a single trailing TextBlock
  THINKING_DELTA  -> appended into a single trailing ThinkingBlock
  TOOL_START      -> a ToolUseBlock
  USAGE           -> folded into the running Usage total
  TURN_END        -> records the stop_reason string ("MAX_TOKENS" etc.)

An empty response yields a single empty TextBlock so the Message is never
content-less (Message.__post_init__ requires >=1 block).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from autoverse.ports import EventSink
from autoverse.types import (
    EventType,
    StreamEvent,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolStartPayload,
    TurnEndPayload,
    UsagePayload,
)
from autoverse.types.messages import (
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)


@dataclass
class UsageAccumulator:
    usage: Usage = field(default_factory=Usage)

    def add(self, other: Usage) -> None:
        self.usage = self.usage + other


@dataclass
class CollectedTurn:
    message: Message
    usage: Usage
    stop_reason: str
    tool_calls: tuple[ToolUseBlock, ...]


async def collect_assistant(
    stream: AsyncIterator[StreamEvent], events: EventSink
) -> CollectedTurn:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolUseBlock] = []
    usage = UsageAccumulator()
    usage_model = ""
    stop_reason = "END_TURN"

    async for ev in stream:
        # TOOL_START and TURN_END are provider-protocol control events here:
        # the former describes the model's requested call (the dispatcher emits
        # the public execution start) and the latter terminates one LLM
        # iteration (the TurnLoop emits the public user-turn end). Forwarding
        # either would produce duplicate tool cards and prematurely split one
        # user turn into several trace records.
        if ev.type not in {EventType.TOOL_START, EventType.TURN_END}:
            await events.emit(ev)
        if ev.type is EventType.TEXT_DELTA:
            payload = ev.payload
            assert isinstance(payload, TextDeltaPayload)
            text_parts.append(payload.text)
        elif ev.type is EventType.THINKING_DELTA:
            tpayload = ev.payload
            assert isinstance(tpayload, ThinkingDeltaPayload)
            thinking_parts.append(tpayload.text)
        elif ev.type is EventType.TOOL_START:
            spayload = ev.payload
            assert isinstance(spayload, ToolStartPayload)
            tool_calls.append(
                ToolUseBlock(
                    id=spayload.tool_use_id,
                    name=spayload.tool_name,
                    input=dict(spayload.tool_input),
                )
            )
        elif ev.type is EventType.USAGE:
            upayload = ev.payload
            assert isinstance(upayload, UsagePayload)
            usage.add(upayload.usage)
            usage_model = upayload.model or usage_model
        elif ev.type is EventType.TURN_END:
            epayload = ev.payload
            assert isinstance(epayload, TurnEndPayload)
            stop_reason = epayload.stop_cause

    blocks: list[ContentBlock] = []
    if thinking_parts:
        blocks.append(ThinkingBlock(text="".join(thinking_parts)))
    if text_parts:
        blocks.append(TextBlock(text="".join(text_parts)))
    blocks.extend(tool_calls)
    if not blocks:
        blocks.append(TextBlock(text=""))

    metadata: dict[str, object] = {"stop_reason": stop_reason}
    if usage_model or usage.usage != Usage():
        metadata["usage"] = {
            "input_tokens": usage.usage.input_tokens,
            "output_tokens": usage.usage.output_tokens,
            "cache_read_tokens": usage.usage.cache_read_tokens,
            "cache_write_tokens": usage.usage.cache_write_tokens,
        }
        metadata["usage_model"] = usage_model
    message = Message(
        role="assistant",
        content=tuple(blocks),
        metadata=metadata,
    )
    return CollectedTurn(
        message=message,
        usage=usage.usage,
        stop_reason=stop_reason,
        tool_calls=tuple(tool_calls),
    )


__all__ = ["UsageAccumulator", "CollectedTurn", "collect_assistant"]
