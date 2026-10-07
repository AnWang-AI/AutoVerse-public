"""WireProvider._build_wire_request — per-wire system & thinking encoding.

Regression coverage for two real-API bugs found against OpenRouter:
- OpenAI wire has no top-level ``system`` field; a bare one is silently
  dropped by the backend, losing the system prompt. It must become a
  ``{"role": "system"}`` message.
- ``thinking_budget`` was never encoded into the wire body, so reasoning
  models never received a reasoning/thinking directive.
"""

from __future__ import annotations

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.fallback import (
    FallbackPlan,
    ModelFallbackStrategy,
    ProtocolSwitchStrategy,
    RetryStrategy,
)
from autoverse.infra.llm.provider import WireProvider
from autoverse.infra.llm.registry import ModelSpec, TransportSpec
from autoverse.infra.llm.router import Router
from autoverse.infra.llm.transport.credential import CredentialResolver, bearer
from autoverse.types import (
    LLMRequest,
    Message,
    ProviderCapabilities,
    TextBlock,
    WireKind,
)


def _spec(wire: WireKind, thinking: bool) -> ModelSpec:
    return ModelSpec(
        model_id="m",
        label="m",
        description="m",
        capabilities=ProviderCapabilities(streaming=True, thinking=thinking),
        transports=(
            TransportSpec(
                id="t",
                wire=wire,
                auth=bearer("KEY"),
                endpoint="local://x",
                real_streaming=True,
            ),
        ),
    )


def _provider(wire: WireKind, thinking: bool = False) -> WireProvider:
    s = _spec(wire, thinking)
    codec = OpenAIWireCodec() if wire is WireKind.OPENAI else AnthropicWireCodec()
    creds = CredentialResolver()
    plan = FallbackPlan(
        retry=RetryStrategy(),
        protocol_switch=ProtocolSwitchStrategy(s),
        model_fallback=ModelFallbackStrategy(s, router=Router(creds)),
    )
    return WireProvider(s, codec, [], fallback=plan, recovery=None)


def _req(system: str = "", thinking_budget: int = 0) -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="hi"),)),),
        system=system,
        model="m",
        max_tokens=64,
        thinking_budget=thinking_budget,
    )


def test_openai_wire_system_becomes_message_not_top_level_field() -> None:
    p = _provider(WireKind.OPENAI)
    body = p._build_wire_request(_req(system="be terse")).body
    assert "system" not in body  # top-level system is ignored by openai api
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1]["role"] == "user"


def test_anthropic_wire_system_is_top_level_field() -> None:
    p = _provider(WireKind.ANTHROPIC)
    body = p._build_wire_request(_req(system="be terse")).body
    assert body["system"] == "be terse"
    # messages must not gain a synthetic system message
    assert all(
        not (isinstance(m, dict) and m.get("role") == "system")
        for m in body["messages"]
    )


def test_openai_wire_encodes_thinking_budget_as_reasoning() -> None:
    p = _provider(WireKind.OPENAI, thinking=True)
    body = p._build_wire_request(_req(thinking_budget=2048)).body
    assert body["reasoning"] == {"max_tokens": 2048}


def test_anthropic_wire_encodes_thinking_budget_as_thinking() -> None:
    p = _provider(WireKind.ANTHROPIC, thinking=True)
    body = p._build_wire_request(_req(thinking_budget=2048)).body
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_thinking_not_encoded_when_capability_disabled() -> None:
    p = _provider(WireKind.OPENAI, thinking=False)
    body = p._build_wire_request(_req(thinking_budget=2048)).body
    assert "reasoning" not in body


def test_thinking_not_encoded_when_budget_zero() -> None:
    p = _provider(WireKind.OPENAI, thinking=True)
    body = p._build_wire_request(_req(thinking_budget=0)).body
    assert "reasoning" not in body


def _cache_provider() -> WireProvider:
    s = ModelSpec(
        model_id="m", label="m", description="m",
        capabilities=ProviderCapabilities(streaming=True, cache=True),
        transports=(TransportSpec(id="t", wire=WireKind.ANTHROPIC, auth=bearer("KEY"),
                                  endpoint="local://x", real_streaming=True),),
    )
    creds = CredentialResolver()
    plan = FallbackPlan(
        retry=RetryStrategy(),
        protocol_switch=ProtocolSwitchStrategy(s),
        model_fallback=ModelFallbackStrategy(s, router=Router(creds)),
    )
    return WireProvider(s, AnthropicWireCodec(), [], fallback=plan, recovery=None)


def test_anthropic_last_tool_gets_cache_control_when_cache_enabled() -> None:
    from autoverse.types import ToolSchema

    p = _cache_provider()
    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="hi"),)),),
        system="", model="m", max_tokens=64,
        tools=(
            ToolSchema(name="a", description="d", json_schema={"type": "object"}),
            ToolSchema(name="b", description="d", json_schema={"type": "object"}),
        ),
    )
    tools = p._build_wire_request(req).body["tools"]
    assert "cache_control" not in tools[0]
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}


def test_usage_event_model_stamped_when_blank() -> None:
    """Regression: codecs emit USAGE with model="" (they don't know the canonical
    id). The provider must stamp it so the meter/trace/TUI can price the turn —
    without this, cost is $0 everywhere because the meter records under "" which
    has no pricing entry."""
    from autoverse.infra.llm.provider import WireProvider
    from autoverse.types import EventType, Usage, UsagePayload, make_event

    blank = make_event(UsagePayload(usage=Usage(input_tokens=10), model=""))
    stamped = WireProvider._stamp_model(blank, "deepseek/deepseek-v4-pro")
    assert stamped.type is EventType.USAGE
    assert stamped.payload.model == "deepseek/deepseek-v4-pro"
    # usage payload is otherwise unchanged
    assert stamped.payload.usage.input_tokens == 10


def test_usage_event_model_not_overwritten_when_present() -> None:
    """If a codec ever does set model, the provider must not clobber it."""
    from autoverse.infra.llm.provider import WireProvider
    from autoverse.types import Usage, UsagePayload, make_event

    ev = make_event(UsagePayload(usage=Usage(input_tokens=5), model="explicit/model"))
    out = WireProvider._stamp_model(ev, "deepseek/deepseek-v4-pro")
    assert out.payload.model == "explicit/model"


def test_stamp_model_passthrough_non_usage_event() -> None:
    """Non-USAGE events pass through untouched."""
    from autoverse.infra.llm.provider import WireProvider
    from autoverse.types import EventType, TextDeltaPayload, make_event

    ev = make_event(TextDeltaPayload(text="hi"))
    out = WireProvider._stamp_model(ev, "deepseek/deepseek-v4-pro")
    assert out is ev and out.type is EventType.TEXT_DELTA


async def test_provider_stream_stamps_model_end_to_end() -> None:
    """End-to-end (Bug A): a USAGE chunk decoded from the wire (model="") must
    emerge from WireProvider.stream carrying the canonical model id, so the
    downstream meter/trace/TUI can price it. Without the stamp the meter records
    under "" → cost $0 everywhere."""
    import pytest
    from autoverse.infra.llm.codec.openai import OpenAIWireCodec
    from autoverse.infra.llm.fallback import (
        FallbackPlan, ModelFallbackStrategy, ProtocolSwitchStrategy, RetryStrategy,
    )
    from autoverse.infra.llm.router import Router
    from autoverse.types import AbortController, EventType
    from .conftest import FakeTransport, fake_spec

    spec = ModelSpec(
        model_id="deepseek/deepseek-v4-pro", label="ds", description="",
        capabilities=ProviderCapabilities(streaming=True, cache=True),
        transports=(fake_spec(),),
    )
    usage_chunk = {
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000, "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
        },
    }
    transport = FakeTransport(spec.transports[0], chunks=[usage_chunk])
    creds = CredentialResolver()
    rs = RetryStrategy(); rs.delay = lambda a: 0.0  # type: ignore[method-assign]
    plan = FallbackPlan(
        retry=rs,
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=Router(creds)),
    )
    provider = WireProvider(spec, OpenAIWireCodec(), [transport], fallback=plan, recovery=None)
    req = LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="hi"),)),),
        system="", model="deepseek", max_tokens=8,   # alias on purpose
    )
    sig = AbortController().signal
    usage_events = [
        ev async for ev in provider.stream(req, sig) if ev.type is EventType.USAGE
    ]
    assert usage_events, "expected a USAGE event"
    u = usage_events[0].payload
    # canonical id stamped (not the alias, not "")
    assert u.model == "deepseek/deepseek-v4-pro"
    # cache buckets disjoint, fed through correctly
    assert u.usage.input_tokens == 200 and u.usage.cache_read_tokens == 800


def test_router_applies_configured_retry_limit() -> None:
    from autoverse.infra.llm.router import Router

    provider = Router(CredentialResolver(), max_retries=0).build(
        "openrouter/deepseek-v4-pro"
    )
    assert isinstance(provider, WireProvider)
    assert provider._fallback.retry.max_retries == 0  # noqa: SLF001
    assert provider._fallback.retry.attempt_limit == 1  # noqa: SLF001
