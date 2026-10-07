"""FallbackPlan: retry backoff / protocol-switch / model-fallback (§7, M7/M8)."""

from __future__ import annotations

import pytest

from autoverse.infra.llm.codec.openai import OpenAIWireCodec
from autoverse.infra.llm.fallback import (
    FallbackPlan,
    ModelFallbackStrategy,
    ProtocolSwitchStrategy,
    RetryStrategy,
)
from autoverse.infra.llm.registry import ModelSpec, TransportSpec
from autoverse.infra.llm.taxonomy import ErrorKind
from autoverse.infra.llm.transport.base import WireRequest
from autoverse.infra.llm.transport.credential import none
from autoverse.types import (
    AbortController,
    EventType,
    LLMRequest,
    Message,
    ProviderCapabilities,
    TextBlock,
    WireKind,
)

from .conftest import FakeTransport, err, fake_spec


def _req() -> LLMRequest:
    return LLMRequest(
        messages=(Message(role="user", content=(TextBlock(text="hi"),)),),
        system="",
        model="m",
    )


def _wire_req() -> WireRequest:
    return WireRequest(body={"messages": []}, stream=True)


def _success_chunk() -> dict[str, object]:
    return {
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]
    }


def _spec(fallback: str | None = None) -> ModelSpec:
    return ModelSpec(
        model_id="m",
        label="M",
        description="",
        capabilities=ProviderCapabilities(),
        transports=(fake_spec(),),
        fallback_model_id=fallback,
    )


def _plan(spec: ModelSpec, router: object = None) -> FallbackPlan:
    rs = RetryStrategy()
    # zero out delay so tests don't sleep
    rs.delay = lambda attempt: 0.0  # type: ignore[method-assign]
    return FallbackPlan(
        retry=rs,
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=router),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_m7_retry_then_success() -> None:
    spec = _spec()
    transport = FakeTransport(
        spec.transports[0],
        chunks=[_success_chunk()],
        errors=[
            err(status=503, kind=ErrorKind.SERVER),
            err(status=503, kind=ErrorKind.SERVER),
        ],
    )
    plan = _plan(spec)
    sig = AbortController().signal
    events = [
        ev
        async for ev in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[transport],
            wire_req=_wire_req(),
        )
    ]
    retries = [e for e in events if e.type is EventType.RETRY]
    assert len(retries) == 2
    assert all(r.payload.error_class == "retryable" for r in retries)
    assert transport.calls == 3
    assert any(e.type is EventType.TEXT_DELTA for e in events)


@pytest.mark.asyncio
async def test_retry_events_are_one_based_and_only_emitted_before_real_retry() -> None:
    spec = _spec()
    transport = FakeTransport(
        spec.transports[0],
        errors=[err(status=429, kind=ErrorKind.RATE_LIMIT)] * 4,
    )
    plan = _plan(spec)
    sig = AbortController().signal
    events = []
    with pytest.raises(Exception) as raised:
        async for event in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[transport],
            wire_req=_wire_req(),
        ):
            events.append(event)
    retries = [event for event in events if event.type is EventType.RETRY]
    assert [event.payload.attempt for event in retries] == [1, 2, 3]
    assert transport.calls == 4
    assert getattr(raised.value, "retry_exhausted", False)


@pytest.mark.asyncio
async def test_mid_stream_failure_is_not_replayed() -> None:
    class PartialTransport(FakeTransport):
        async def send(self, req, signal, timeout):  # type: ignore[no-untyped-def]
            self.calls += 1
            yield {
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ]
            }
            raise err(status=503, kind=ErrorKind.SERVER)

    spec = _spec()
    transport = PartialTransport(spec.transports[0])
    plan = _plan(spec)
    sig = AbortController().signal
    events = []
    with pytest.raises(Exception) as raised:
        async for event in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[transport],
            wire_req=_wire_req(),
        ):
            events.append(event)
    assert transport.calls == 1
    assert any(event.type is EventType.TEXT_DELTA for event in events)
    assert not any(event.type is EventType.RETRY for event in events)
    assert getattr(raised.value, "response_started", False)


@pytest.mark.asyncio
async def test_permanent_emits_error_and_stops() -> None:
    spec = _spec()
    transport = FakeTransport(
        spec.transports[0], errors=[err(status=400, kind=ErrorKind.BAD_REQUEST)]
    )
    plan = _plan(spec)
    sig = AbortController().signal
    with pytest.raises(Exception) as raised:
        _ = [
            ev
            async for ev in plan.run(
                _req(),
                sig,
                codec=OpenAIWireCodec(),
                transports=[transport],
                wire_req=_wire_req(),
            )
        ]
    assert transport.calls == 1
    assert getattr(raised.value, "kind", None) is ErrorKind.BAD_REQUEST


@pytest.mark.asyncio
async def test_fallback_trigger_switches_transport() -> None:
    spec = ModelSpec(
        model_id="m",
        label="M",
        description="",
        capabilities=ProviderCapabilities(),
        transports=(fake_spec(sid="t1"), fake_spec(sid="t2")),
    )
    t1 = FakeTransport(spec.transports[0], errors=[err(status=401, kind=ErrorKind.AUTH)])
    t2 = FakeTransport(spec.transports[1], chunks=[_success_chunk()])
    plan = _plan(spec)
    sig = AbortController().signal
    events = [
        ev
        async for ev in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[t1, t2],
            wire_req=_wire_req(),
        )
    ]
    assert t1.calls == 1
    assert t2.calls == 1
    assert any(e.type is EventType.TEXT_DELTA for e in events)


class _StubRouter:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    def build(self, model_name: str) -> object:
        return self._provider


@pytest.mark.asyncio
async def test_m8_model_fallback_marks_next_model() -> None:
    from autoverse.infra.llm.echo import EchoProvider

    spec = _spec(fallback="gpt-4o")
    transport = FakeTransport(
        spec.transports[0], errors=[err(status=401, kind=ErrorKind.AUTH)]
    )
    fallback_provider = EchoProvider(reply="fallback-reply")
    plan = _plan(spec, router=_StubRouter(fallback_provider))
    sig = AbortController().signal
    events = [
        ev
        async for ev in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[transport],
            wire_req=_wire_req(),
        )
    ]
    retries = [e for e in events if e.type is EventType.RETRY]
    next_models = [r.payload.next_model for r in retries if r.payload.next_model]
    assert "gpt-4o" in next_models
    # fallback provider's reply flows through
    texts = [e.payload.text for e in events if e.type is EventType.TEXT_DELTA]
    assert "fallback-reply" in texts


@pytest.mark.asyncio
async def test_global_attempt_cap_across_transports() -> None:
    """§7: per-channel attempt budget PLUS a global cap on total attempts.

    With N transports each RETRYABLE-failing forever, total attempts must be
    bounded by the global cap, not N * (max_retries + 1).
    """
    specs = tuple(fake_spec(sid=f"t{i}") for i in range(4))
    spec = ModelSpec(
        model_id="m",
        label="M",
        description="",
        capabilities=ProviderCapabilities(),
        transports=specs,
    )
    # Each transport always fails RETRYABLE (server 503), 8 scripted errors so
    # it never runs out before any plausible cap.
    transports = [
        FakeTransport(s, errors=[err(status=503, kind=ErrorKind.SERVER)] * 8)
        for s in specs
    ]
    rs = RetryStrategy(global_max_attempts=5)
    rs.delay = lambda attempt: 0.0  # type: ignore[method-assign]
    plan = FallbackPlan(
        retry=rs,
        protocol_switch=ProtocolSwitchStrategy(spec),
        model_fallback=ModelFallbackStrategy(spec, router=None),
    )
    sig = AbortController().signal
    with pytest.raises(Exception) as raised:
        _ = [
            ev
            async for ev in plan.run(
                _req(),
                sig,
                codec=OpenAIWireCodec(),
                transports=transports,
                wire_req=_wire_req(),
            )
        ]
    total_calls = sum(t.calls for t in transports)
    # Without a global cap this would be 4 * (3 + 1) = 16.
    assert total_calls == 5
    assert getattr(raised.value, "retry_exhausted", False)


@pytest.mark.asyncio
async def test_protocol_switch_clears_account_id() -> None:
    """§12: switching transport clears any captured account_id (fresh routing)."""

    class PinnedFake(FakeTransport):
        def __init__(self, spec: TransportSpec) -> None:
            super().__init__(spec, errors=[err(status=401, kind=ErrorKind.AUTH)])
            self.account_id = "stale-node"
            self.reset_calls = 0

        def reset_pinning(self) -> None:
            self.account_id = ""
            self.reset_calls += 1

    specs = (fake_spec(sid="t1"), fake_spec(sid="t2"))
    spec = ModelSpec(
        model_id="m",
        label="M",
        description="",
        capabilities=ProviderCapabilities(),
        transports=specs,
    )
    t1 = PinnedFake(specs[0])
    t2 = FakeTransport(specs[1], chunks=[_success_chunk()])
    plan = _plan(spec)
    sig = AbortController().signal
    _ = [
        ev
        async for ev in plan.run(
            _req(),
            sig,
            codec=OpenAIWireCodec(),
            transports=[t1, t2],
            wire_req=_wire_req(),
        )
    ]
    # On FALLBACK_TRIGGER switch away from t1, its captured account_id is cleared.
    assert t1.reset_calls == 1
    assert t1.account_id == ""
