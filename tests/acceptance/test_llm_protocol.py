"""Acceptance M1-M10 — LLM & protocol (docs/reference/testing.md §4).

Each scenario corresponds to a row in the M1-M10 table. FakeTransport and
golden fixtures are reused from tests.unit.llm.
"""

from __future__ import annotations

import pytest

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.base import DecodeState
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.fallback import (
    FallbackPlan,
    ModelFallbackStrategy,
    ProtocolSwitchStrategy,
    RetryStrategy,
)
from autoverse.infra.llm.provider import WireProvider
from autoverse.infra.llm.registry import (
    ModelSpec,
    TransportSpec,
    get_spec,
    resolve_alias,
)
from autoverse.infra.llm.streaming import SingleShotAdapter
from autoverse.infra.llm.taxonomy import ErrorKind
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.infra.llm.transport.credential import bearer, hmac_sha1
from autoverse.ports import LLMProvider
from autoverse.types import (
    AbortController,
    EventType,
    ImageBlock,
    LLMRequest,
    Message,
    ProviderCapabilities,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    WireKind,
)

from tests.unit.llm import golden
from tests.unit.llm.conftest import FakeTransport, err, fake_spec


# ── shared builders ──────────────────────────────────────────────────────────
def _messages() -> list[Message]:
    return [
        Message(role="user", content=(TextBlock(text="What's the weather?"),)),
        Message(
            role="assistant",
            content=(
                TextBlock(text="Let me check."),
                ToolUseBlock(id="toolu_01", name="get_weather", input={"city": "Paris"}),
            ),
        ),
        Message(
            role="user",
            content=(ToolResultBlock(tool_use_id="toolu_01", content="Sunny, 22C"),),
        ),
    ]


def _llm_req(model: str = "m") -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="hi"),)),),
        system="",
        model=model,
    )


def _wire_req() -> WireRequest:
    return WireRequest(body={"messages": []}, stream=True)


def _success_chunk() -> dict[str, object]:
    return {
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]
    }


def _no_delay_plan(spec: ModelSpec, router: object = None) -> FallbackPlan:
    rs = RetryStrategy()
    rs.delay = lambda attempt: 0.0  # type: ignore[method-assign]
    return FallbackPlan(
        retry=rs,
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=router),  # type: ignore[arg-type]
    )


# ── M1: codec encode round-trip ──────────────────────────────────────────────
def test_m1_codec_encode_matches_golden() -> None:
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
    assert codec.encode_messages(_messages()) == expected


# ── M2: codec decode round-trip ──────────────────────────────────────────────
def test_m2_codec_decode_matches_expected() -> None:
    codec = AnthropicWireCodec()
    st = DecodeState()
    events = []
    for chunk in golden.load_jsonl("anthropic", "decode_text_tool.jsonl"):
        events.extend(codec.decode_chunk(chunk, st))
    types = [e.type for e in events]
    assert types == [
        EventType.USAGE,
        EventType.TEXT_DELTA,
        EventType.TEXT_DELTA,
        EventType.TOOL_START,
        EventType.USAGE,
        EventType.TURN_END,
    ]


# ── M3: openai codec symmetry ────────────────────────────────────────────────
def test_m3_openai_symmetry_tool_use() -> None:
    codec = OpenAIWireCodec()
    st = DecodeState()
    events = []
    for chunk in golden.load_jsonl("openai", "decode_text_tool.jsonl"):
        events.extend(codec.decode_chunk(chunk, st))
    tool = next(e.payload for e in events if e.type is EventType.TOOL_START)
    assert tool.tool_use_id == "call_01"
    assert tool.tool_name == "get_weather"
    assert tool.tool_input == {"city": "Paris"}


# ── M4: non-streaming wrapping ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_m4_single_shot_adapter() -> None:
    spec = fake_spec(wire=WireKind.OPENAI, real_streaming=False)
    whole = {
        "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    adapter = SingleShotAdapter(FakeTransport(spec, chunks=[whole]), OpenAIWireCodec())
    sig = AbortController().signal
    events = [ev async for ev in adapter.send_decoded(_wire_req(), sig, 1.0)]
    assert [e.type for e in events][-1] is EventType.TURN_END
    assert any(e.type is EventType.TEXT_DELTA for e in events)


# ── M5: one model, multiple transports, same codec ───────────────────────────
@pytest.mark.asyncio
async def test_m5_one_model_two_transports_same_codec() -> None:
    hmac_ts = TransportSpec(
        id="m5.hmac",
        wire=WireKind.OPENAI,
        auth=hmac_sha1("APP_ID", "APP_KEY", "src"),
        endpoint="local://hmac",
    )
    bearer_ts = TransportSpec(
        id="m5.bearer",
        wire=WireKind.OPENAI,
        auth=bearer("TOK"),
        endpoint="local://bearer",
    )
    spec = ModelSpec(
        model_id="m5",
        label="M5",
        description="",
        capabilities=ProviderCapabilities(),
        transports=(hmac_ts, bearer_ts),
    )
    codec = OpenAIWireCodec()
    # first transport fails to auth → switches to second; SAME codec object used.
    t1 = FakeTransport(hmac_ts, errors=[err(status=401, kind=ErrorKind.AUTH)])
    t2 = FakeTransport(bearer_ts, chunks=[_success_chunk()])
    provider = WireProvider(
        spec,
        codec,
        [t1, t2],
        fallback=_no_delay_plan(spec),
        recovery=None,
    )
    sig = AbortController().signal
    events = [ev async for ev in provider.stream(_llm_req("m5"), sig)]
    assert t1.spec.id != t2.spec.id  # different auth/endpoint
    assert t1.calls == 1 and t2.calls == 1
    assert any(e.type is EventType.TEXT_DELTA for e in events)


# ── M6: vision declarative rejection ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_m6_vision_rejected_in_stream() -> None:
    spec = ModelSpec(
        model_id="m6",
        label="M6",
        description="",
        capabilities=ProviderCapabilities(vision=False),
        transports=(fake_spec(),),
    )
    transport = FakeTransport(spec.transports[0], chunks=[_success_chunk()])
    provider = WireProvider(
        spec, OpenAIWireCodec(), [transport], fallback=_no_delay_plan(spec), recovery=None
    )
    req = LLMRequest(
        messages=(
            Message(
                role="user",
                content=(
                    TextBlock(text="see"),
                    ImageBlock(media_type="image/png", data="QQ=="),
                ),
            ),
        ),
        system="",
        model="m6",
    )
    sig = AbortController().signal
    from autoverse.infra.llm.taxonomy import LLMError

    with pytest.raises(LLMError) as ei:
        [ev async for ev in provider.stream(req, sig)]
    assert ei.value.kind is ErrorKind.UNSUPPORTED


# ── M7: retry with exponential backoff ───────────────────────────────────────
@pytest.mark.asyncio
async def test_m7_retry_two_then_success() -> None:
    spec = ModelSpec(
        model_id="m7",
        label="M7",
        description="",
        capabilities=ProviderCapabilities(),
        transports=(fake_spec(),),
    )
    transport = FakeTransport(
        spec.transports[0],
        chunks=[_success_chunk()],
        errors=[
            err(kind=ErrorKind.TIMEOUT),
            err(kind=ErrorKind.TIMEOUT),
        ],
    )
    provider = WireProvider(
        spec, OpenAIWireCodec(), [transport], fallback=_no_delay_plan(spec), recovery=None
    )
    sig = AbortController().signal
    events = [ev async for ev in provider.stream(_llm_req("m7"), sig)]
    retries = [e for e in events if e.type is EventType.RETRY]
    assert len(retries) == 2
    assert transport.calls == 3
    assert any(e.type is EventType.TEXT_DELTA for e in events)


# ── M8: model fallback ───────────────────────────────────────────────────────
class _StubRouter:
    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def build(self, model_name: str) -> LLMProvider:
        return self._provider


@pytest.mark.asyncio
async def test_m8_model_fallback_next_model() -> None:
    from autoverse.infra.llm.echo import EchoProvider

    spec = ModelSpec(
        model_id="m8",
        label="M8",
        description="",
        capabilities=ProviderCapabilities(),
        transports=(fake_spec(),),
        fallback_model_id="echo-fallback",
    )
    transport = FakeTransport(
        spec.transports[0], errors=[err(status=403, kind=ErrorKind.AUTH)]
    )
    fb = EchoProvider(reply="degraded")
    provider = WireProvider(
        spec,
        OpenAIWireCodec(),
        [transport],
        fallback=_no_delay_plan(spec, router=_StubRouter(fb)),
        recovery=None,
    )
    sig = AbortController().signal
    events = [ev async for ev in provider.stream(_llm_req("m8"), sig)]
    next_models = [
        e.payload.next_model
        for e in events
        if e.type is EventType.RETRY and e.payload.next_model
    ]
    assert "echo-fallback" in next_models
    assert "degraded" in [
        e.payload.text for e in events if e.type is EventType.TEXT_DELTA
    ]


# ── M9: prompt caching ───────────────────────────────────────────────────────
def test_m9_cache_control_and_usage() -> None:
    codec = AnthropicWireCodec()
    system = "STATIC GUIDELINES\n" * 700 + "## Working Directory\n/tmp"
    encoded = codec.encode_system(system, cache=True)
    assert isinstance(encoded, list)
    assert encoded[0]["cache_control"] == {"type": "ephemeral"}
    st = DecodeState()
    usage_events = codec.decode_chunk(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 30,
                }
            },
        },
        st,
    )
    usage = usage_events[0].payload.usage
    assert usage.cache_read_tokens == 50
    assert usage.cache_write_tokens == 30


# ── M10: add a new backend by registry only ──────────────────────────────────
@pytest.mark.asyncio
async def test_m10_new_backend_routes_without_touching_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autoverse.infra.llm.registry as reg
    from autoverse.infra.llm.router import Router
    from autoverse.infra.llm.transport.credential import CredentialResolver

    new_spec = ModelSpec(
        model_id="new-backend",
        label="New",
        description="reuse openai codec + bearer transport",
        capabilities=ProviderCapabilities(),
        transports=(
            TransportSpec(
                id="new.openai.bearer",
                wire=WireKind.OPENAI,
                auth=bearer("NEW_KEY"),
                endpoint="https://new.example.com/v1/chat/completions",
            ),
        ),
    )
    # register the new spec into the live registry tables (registry-only change).
    monkeypatch.setitem(reg.MODEL_REGISTRY, "new-backend", new_spec)

    router = Router(CredentialResolver())
    provider = router.build("new-backend")
    assert isinstance(provider, LLMProvider)
    assert provider.capabilities is new_spec.capabilities

    # other backends unchanged: existing spec still resolves correctly.
    assert (
        get_spec(resolve_alias("deepseek/deepseek-v4-pro"))
        is reg.MODEL_REGISTRY["deepseek/deepseek-v4-pro"]
    )

    # new model is streamable end-to-end via a FakeTransport substitute.
    transport = FakeTransport(new_spec.transports[0], chunks=[_success_chunk()])
    fp = WireProvider(
        new_spec,
        OpenAIWireCodec(),
        [transport],
        fallback=_no_delay_plan(new_spec),
        recovery=None,
    )
    sig = AbortController().signal
    events = [ev async for ev in fp.stream(_llm_req("new-backend"), sig)]
    assert any(e.type is EventType.TEXT_DELTA for e in events)
