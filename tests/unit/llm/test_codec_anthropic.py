"""M1/M2/M6/M9 — Anthropic codec encode/decode round-trip + vision + cache."""

from __future__ import annotations

import pytest

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.base import DecodeState
from autoverse.infra.llm.taxonomy import ErrorKind, LLMError
from autoverse.types import (
    EventType,
    Message,
    ProviderCapabilities,
    TextBlock,
    ToolSchema,
    Usage,
)

from .conftest import FakeTransport  # noqa: F401  (ensures conftest importable)
from . import golden


def test_m1_encode_messages(sample_messages: list[Message]) -> None:
    codec = AnthropicWireCodec()
    expected = [
        {"role": "user", "content": [{"type": "text", "text": "What's the weather?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": "Sunny, 22C",
                    "is_error": False,
                }
            ],
        },
    ]
    assert codec.encode_messages(sample_messages) == expected


def test_encode_tools() -> None:
    codec = AnthropicWireCodec()
    tools = [
        ToolSchema(name="t", description="d", json_schema={"type": "object"})
    ]
    assert codec.encode_tools(tools) == [
        {"name": "t", "description": "d", "input_schema": {"type": "object"}}
    ]


def test_merge_adjacent_same_role() -> None:
    codec = AnthropicWireCodec()
    msgs = [
        Message(role="user", content=(TextBlock(text="a"),)),
        Message(role="user", content=(TextBlock(text="b"),)),
    ]
    out = codec.encode_messages(msgs)
    assert len(out) == 1
    assert out[0]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]


def test_m2_decode_round_trip() -> None:
    codec = AnthropicWireCodec()
    st = DecodeState()
    events = []
    for chunk in golden.load_jsonl("anthropic", "decode_text_tool.jsonl"):
        events.extend(codec.decode_chunk(chunk, st))
    seq = [(e.type, e.payload) for e in events]
    types = [t for t, _ in seq]
    assert types == [
        EventType.USAGE,
        EventType.TEXT_DELTA,
        EventType.TEXT_DELTA,
        EventType.TOOL_START,
        EventType.USAGE,
        EventType.TURN_END,
    ]
    tool_ev = next(p for t, p in seq if t is EventType.TOOL_START)
    assert tool_ev.tool_use_id == "toolu_01"
    assert tool_ev.tool_name == "get_weather"
    assert tool_ev.tool_input == {"city": "Paris"}
    turn = next(p for t, p in seq if t is EventType.TURN_END)
    assert turn.stop_cause == "TOOL_USE"


def test_m6_vision_rejected_when_unsupported(image_message: Message) -> None:
    codec = AnthropicWireCodec()
    caps = ProviderCapabilities(vision=False)
    with pytest.raises(LLMError) as ei:
        codec.supports(image_message.content, caps)
    assert ei.value.kind is ErrorKind.UNSUPPORTED
    assert "vision" in ei.value.message


def test_m6_vision_accepted_when_supported(image_message: Message) -> None:
    codec = AnthropicWireCodec()
    caps = ProviderCapabilities(vision=True)
    # no raise
    codec.supports(image_message.content, caps)


def test_m9_cache_control_injection() -> None:
    codec = AnthropicWireCodec()
    static = "TOOLS AND GUIDELINES\n" * 600  # > cache min threshold
    system = static + "## Working Directory\n/tmp"
    out = codec.encode_system(system, cache=True)
    assert isinstance(out, list)
    assert out[0]["cache_control"] == {"type": "ephemeral"}
    assert "Working Directory" in out[1]["text"]
    assert "cache_control" not in out[1]


def test_m9_cache_skipped_when_short() -> None:
    codec = AnthropicWireCodec()
    out = codec.encode_system("short system", cache=True)
    assert out == "short system"


def test_m9_usage_cache_fields_decoded() -> None:
    codec = AnthropicWireCodec()
    st = DecodeState()
    chunk = {
        "type": "message_start",
        "message": {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 0,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 40,
            }
        },
    }
    events = codec.decode_chunk(chunk, st)
    usage = events[0].payload.usage
    assert isinstance(usage, Usage)
    assert usage.cache_read_tokens == 100
    assert usage.cache_write_tokens == 40


def test_duplicate_content_block_stop_emits_tool_start_once() -> None:
    """Regression: a repeated content_block_stop for the same tool index must
    not re-emit TOOL_START (mirrors the OpenAI codec idempotency guard)."""
    codec = AnthropicWireCodec()
    st = DecodeState()
    chunks = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "f"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"x": 1}'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_stop", "index": 0},
    ]
    events = []
    for c in chunks:
        events.extend(codec.decode_chunk(c, st))
    starts = [e for e in events if e.type is EventType.TOOL_START]
    assert len(starts) == 1
    assert starts[0].payload.tool_name == "f"
    assert starts[0].payload.tool_input == {"x": 1}
