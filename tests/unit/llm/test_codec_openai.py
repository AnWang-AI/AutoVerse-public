"""M3 — OpenAI codec symmetry: encode/decode, tool calls map to unified ToolUseBlock."""

from __future__ import annotations

import json

from autoverse.infra.llm.codec.base import DecodeState
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.types import (
    EventType,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)

from . import golden


def test_m3_encode_messages(sample_messages: list[Message]) -> None:
    codec = OpenAIWireCodec()
    out = codec.encode_messages(sample_messages)
    assert out[0] == {"role": "user", "content": "What's the weather?"}
    assistant = out[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me check."
    tcs = assistant["tool_calls"]
    assert tcs[0]["id"] == "toolu_01"
    assert tcs[0]["function"]["name"] == "get_weather"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"city": "Paris"}
    assert out[2] == {
        "role": "tool",
        "tool_call_id": "toolu_01",
        "content": "Sunny, 22C",
    }


def test_tool_result_merged_with_text_emits_tool_before_user() -> None:
    """When an interrupted tool's placeholder result is merged with the user's
    next prompt into one user message, the ``tool`` message MUST be emitted
    before the user text so it stays adjacent to the assistant tool_calls
    (regression for HTTP 400 'insufficient tool messages following tool_calls
    message')."""
    codec = OpenAIWireCodec()
    msgs = [
        Message(role="user", content=(TextBlock(text="go"),)),
        Message(
            role="assistant",
            content=(ToolUseBlock(id="t1", name="bash", input={"cmd": "ls"}),),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(tool_use_id="t1", content="(no result captured)", is_error=True),
                TextBlock(text="new prompt after Ctrl+C"),
            ),
        ),
    ]
    out = codec.encode_messages(msgs)
    # order: user(go), assistant(tool_calls), tool(t1), user(new prompt)
    assert out[1]["role"] == "assistant"
    assert "tool_calls" in out[1]
    assert out[2] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": "(no result captured)",
    }
    assert out[3] == {"role": "user", "content": "new prompt after Ctrl+C"}


def test_m3_encode_tools() -> None:
    codec = OpenAIWireCodec()
    tools = [ToolSchema(name="t", description="d", json_schema={"type": "object"})]
    assert codec.encode_tools(tools) == [
        {
            "type": "function",
            "function": {
                "name": "t",
                "description": "d",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_m3_decode_round_trip() -> None:
    codec = OpenAIWireCodec()
    st = DecodeState()
    events = []
    for chunk in golden.load_jsonl("openai", "decode_text_tool.jsonl"):
        events.extend(codec.decode_chunk(chunk, st))
    seq = [(e.type, e.payload) for e in events]
    types = [t for t, _ in seq]
    assert EventType.TEXT_DELTA in types
    assert EventType.TOOL_START in types
    assert types[-1] is EventType.TURN_END
    tool_ev = next(p for t, p in seq if t is EventType.TOOL_START)
    assert tool_ev.tool_use_id == "call_01"
    assert tool_ev.tool_name == "get_weather"
    assert tool_ev.tool_input == {"city": "Paris"}
    turn = next(p for t, p in seq if t is EventType.TURN_END)
    assert turn.stop_cause == "TOOL_USE"


def test_decode_bad_args_degrades_to_empty() -> None:
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {"name": "f", "arguments": "{bad"},
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = []
    for c in chunks:
        events.extend(codec.decode_chunk(c, st))
    tool_ev = next(e.payload for e in events if e.type is EventType.TOOL_START)
    assert tool_ev.tool_input == {}


def test_reasoning_content_maps_to_thinking() -> None:
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {"choices": [{"index": 0, "delta": {"reasoning_content": "hmm"}}]}
    events = codec.decode_chunk(chunk, st)
    assert events[0].type is EventType.THINKING_DELTA
    assert events[0].payload.text == "hmm"


def test_duplicate_finish_chunk_emits_tool_start_once() -> None:
    """Regression: some OpenRouter providers (e.g. StreamLake/deepseek-v4-pro)
    emit two chunks with finish_reason=='tool_calls' for a single tool call.
    TOOL_START must be emitted exactly once per tool slot (idempotent finish)."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_00",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city": "Paris"}'}}
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = []
    for c in chunks:
        events.extend(codec.decode_chunk(c, st))
    starts = [e for e in events if e.type is EventType.TOOL_START]
    assert len(starts) == 1
    assert starts[0].payload.tool_name == "get_weather"
    assert starts[0].payload.tool_input == {"city": "Paris"}


def test_split_finish_and_usage_emits_single_turn_end() -> None:
    """Regression (found against live deepseek-v4-pro via OpenRouter): the
    backend sends TWO terminal chunks both carrying finish_reason=='stop' — the
    first with the content delta, the second with the trailing usage block.
    TURN_END is the terminal event of a turn and must be emitted EXACTLY once;
    the late USAGE must still be surfaced."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "PONG"}, "finish_reason": "stop"}]},
        {
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2},
        },
    ]
    events = []
    for c in chunks:
        events.extend(codec.decode_chunk(c, st))
    turn_ends = [e for e in events if e.type is EventType.TURN_END]
    usages = [e for e in events if e.type is EventType.USAGE]
    assert len(turn_ends) == 1, f"expected exactly one TURN_END, got {len(turn_ends)}"
    assert turn_ends[0].payload.stop_cause == "END_TURN"
    # the trailing usage chunk is still surfaced even though TURN_END fired first
    assert len(usages) == 1
    assert usages[0].payload.usage.input_tokens == 8
    assert usages[0].payload.usage.output_tokens == 2


def test_duplicate_tool_finish_chunk_emits_turn_end_once() -> None:
    """Two finish_reason=='tool_calls' chunks must still yield a single
    TURN_END (companion to the single-TOOL_START guarantee)."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_00",
                                "function": {"name": "f", "arguments": "{}"},
                            }
                        ]
                    },
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events = []
    for c in chunks:
        events.extend(codec.decode_chunk(c, st))
    assert sum(1 for e in events if e.type is EventType.TURN_END) == 1


def test_openrouter_reasoning_field_maps_to_thinking() -> None:
    """Regression: OpenRouter normalises the reasoning field to ``reasoning``
    (not the OpenAI/DeepSeek-native ``reasoning_content``). Both must map to
    THINKING_DELTA, otherwise reasoning-model thinking is silently dropped."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {"choices": [{"index": 0, "delta": {"reasoning": "let me think"}}]}
    events = codec.decode_chunk(chunk, st)
    assert events[0].type is EventType.THINKING_DELTA
    assert events[0].payload.text == "let me think"


def test_cached_tokens_subtracted_from_input() -> None:
    """Regression: OpenAI-wire ``prompt_tokens`` is the FULL prompt including the
    cached prefix, while ``cached_tokens`` is a subset. Internal Usage keeps the
    buckets disjoint (cache_read separate from input) so the meter does not bill
    cached tokens at input + cache-read price, and the sidebar does not
    double-count them."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
        },
    }
    events = codec.decode_chunk(chunk, st)
    usages = [e for e in events if e.type is EventType.USAGE]
    assert len(usages) == 1
    u = usages[0].payload.usage
    # 1000 total prompt - 800 cached = 200 fresh input
    assert u.input_tokens == 200
    assert u.cache_read_tokens == 800
    # disjoint buckets: fresh input + cache_read reconstructs the full prompt
    assert u.input_tokens + u.cache_read_tokens == 1000


def test_cache_write_tokens_decoded() -> None:
    """Regression: cache_write_tokens was hard-coded to 0; providers that report
    it (e.g. native DeepSeek) must have the write counter surfaced."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 500},
        },
    }
    events = codec.decode_chunk(chunk, st)
    u = [e for e in events if e.type is EventType.USAGE][0].payload.usage
    assert u.cache_write_tokens == 500
    assert u.input_tokens == 500  # nothing cached → full prompt is fresh


def test_cached_tokens_clamped_when_exceeding_prompt() -> None:
    """Defensive: a provider reporting cached > prompt must not yield a negative
    input_tokens."""
    codec = OpenAIWireCodec()
    st = DecodeState()
    chunk = {
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 999},
        },
    }
    events = codec.decode_chunk(chunk, st)
    u = [e for e in events if e.type is EventType.USAGE][0].payload.usage
    assert u.input_tokens == 0
    assert u.cache_read_tokens == 999
