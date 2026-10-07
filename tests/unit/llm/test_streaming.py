"""SingleShotAdapter: non-streaming whole-response → unified event stream (M4)."""

from __future__ import annotations

import pytest

from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.streaming import SingleShotAdapter
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.types import AbortController, EventType, WireKind

from .conftest import FakeTransport, fake_spec


def _whole_openai_response() -> dict[str, object]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"content": "Hi there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


@pytest.mark.asyncio
async def test_m4_single_shot_adapter_decodes_whole_response() -> None:
    spec = fake_spec(wire=WireKind.OPENAI, real_streaming=False, sid="ss.t")
    transport = FakeTransport(spec, chunks=[_whole_openai_response()])
    adapter = SingleShotAdapter(transport, OpenAIWireCodec())
    sig = AbortController().signal
    req = WireRequest(body={}, stream=False)
    events = [
        ev async for ev in adapter.send_decoded(req, sig, timeout=1.0)
    ]
    types = [e.type for e in events]
    assert EventType.TEXT_DELTA in types
    assert EventType.USAGE in types
    assert types[-1] is EventType.TURN_END
    text = next(e.payload.text for e in events if e.type is EventType.TEXT_DELTA)
    assert text == "Hi there"


def test_single_shot_adapter_exposes_spec() -> None:
    spec = fake_spec(real_streaming=False)
    adapter = SingleShotAdapter(FakeTransport(spec), OpenAIWireCodec())
    assert adapter.spec is spec
