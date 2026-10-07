"""Contract tests for shared value types and ports.

Guard the invariants described by docs/architecture/contracts.md. These tests
depend only on the contract layer.

NOTE on C3: older acceptance text described NETWORK as a tier, but the current
authoritative definition is the `CapabilityTier` enum in
autoverse.types.capability. It defines a 4-tier linear chain with network as an
orthogonal flag on tools and sub-agent definitions (docs/architecture/contracts.md).
"""

from __future__ import annotations

import dataclasses

import pytest

from autoverse import types as av
from autoverse.types import events as events_mod
from autoverse.types import tools as tools_mod


# ── C1: EventType full coverage ──────────────────────────────────────────────
def test_c1_eventtype_full_coverage() -> None:
    for member in av.EventType:
        assert member in av.PAYLOAD_FOR, f"{member} missing from PAYLOAD_FOR"
    assert len(av.PAYLOAD_FOR) == len(list(av.EventType))
    # exactly one payload type per member
    assert len(set(av.PAYLOAD_FOR.values())) == len(av.PAYLOAD_FOR)


# ── C2: make_event reverse lookup ────────────────────────────────────────────
def test_c2_make_event_reverse_lookup() -> None:
    samples = {
        av.EventType.TEXT_DELTA: av.TextDeltaPayload(text="hi"),
        av.EventType.THINKING_DELTA: av.ThinkingDeltaPayload(text="t"),
        av.EventType.TOOL_START: av.ToolStartPayload("id", "read", {}),
        av.EventType.TOOL_END: av.ToolEndPayload("id", "read", "out"),
        av.EventType.COMPACTING: av.CompactingPayload("micro"),
        av.EventType.GATE_DECISION: av.GatePayload("completion", "stop"),
        av.EventType.USAGE: av.UsagePayload(av.Usage(), "m"),
        av.EventType.TURN_START: av.TurnStartPayload(0, 1, "m"),
        av.EventType.TURN_END: av.TurnEndPayload(0, 1, "end_turn"),
        av.EventType.SUBAGENT_ACTIVITY: av.SubagentActivityPayload("j", "scout", "a"),
        av.EventType.SUBAGENT_CHILD: av.SubagentChildPayload(
            "j", "scout", av.make_event(av.TextDeltaPayload(text="hi"))
        ),
        av.EventType.ERROR: av.ErrorPayload("boom"),
        av.EventType.RETRY: av.RetryPayload(1, "timeout"),
        av.EventType.APPROVAL: av.ApprovalPayload("c", "delete", "rm"),
        av.EventType.REQUEST: av.RequestPayload(0, "s", "h", (), "th", "m", 1, 1.0, 0),
    }
    # every EventType has a sample (also re-checks coverage)
    assert set(samples) == set(av.EventType)
    for etype, payload in samples.items():
        ev = av.make_event(payload)
        assert ev.type == etype
        assert av.PAYLOAD_FOR[ev.type] is type(payload)

    class Bogus:
        pass

    with pytest.raises(TypeError):
        av.make_event(Bogus())  # type: ignore[arg-type]


def test_c2_streamevent_rejects_mismatch() -> None:
    with pytest.raises(TypeError):
        av.StreamEvent(av.EventType.TEXT_DELTA, av.UsagePayload(av.Usage(), "m"))


# ── C3: CapabilityTier linear order ──────────────────────────────────────────
def test_c3_capability_tier_linear() -> None:
    t = av.CapabilityTier
    assert t.READ_ONLY < t.CONTROLLED_WRITE < t.ARBITRARY_WRITE < t.PROCESS
    assert len(list(t)) == 4
    # higher tier includes lower (int order)
    assert int(t.PROCESS) == 3 and int(t.READ_ONLY) == 0


# ── C4: Decision.deny must carry reason ──────────────────────────────────────
def test_c4_decision_deny_reason() -> None:
    d = av.Decision.deny("nope")
    assert d.action == "deny" and d.reason == "nope"
    assert av.Decision.allow().allowed is True
    with pytest.raises(ValueError):
        av.Decision(action="deny", reason=None)


# ── C5: ToolResult factories ─────────────────────────────────────────────────
def test_c5_toolresult_factories() -> None:
    ok = av.ToolResult.ok("done", file_path="/x")
    assert ok.succeeded and not ok.is_error
    assert ok.metadata["file_path"] == "/x"
    err = av.ToolResult.error("bad", error_kind="timeout", detail=1)
    assert err.is_error and not err.succeeded
    assert err.error_kind == "timeout"
    assert err.metadata["detail"] == 1


# ── C6: Abort two-level idempotent ───────────────────────────────────────────
def test_c6_abort_idempotent() -> None:
    c = av.AbortController()
    assert not c.signal.aborted
    c.abort("interrupt")
    c.abort("cancel")
    assert c.signal.aborted
    assert c.signal.reason == "interrupt"  # first reason wins


# ── C7: Usage aggregation ────────────────────────────────────────────────────
def test_c7_usage_aggregation() -> None:
    a = av.Usage(input_tokens=10, output_tokens=5, cache_read_tokens=2)
    b = av.Usage(input_tokens=3, output_tokens=7, cache_write_tokens=1)
    s = a + b
    assert s.input_tokens == 13 and s.output_tokens == 12
    assert s.cache_read_tokens == 2 and s.cache_write_tokens == 1
    assert s.total == s.input_tokens + s.output_tokens


# ── C8: Port structural subtyping ────────────────────────────────────────────
def test_c8_port_structural_subtyping() -> None:
    from typing import AsyncIterator

    from autoverse.ports import (
        EventSink,
        LLMProvider,
        PermissionPort,
        SecretProvider,
        Store,
    )
    from autoverse.types import (
        Decision,
        LLMRequest,
        Message,
        ProviderCapabilities,
        StreamEvent,
        ToolCall,
    )

    class MinSink:
        async def emit(self, event: StreamEvent) -> None: ...

    class MinPerm:
        async def check(self, call: ToolCall) -> Decision:
            return Decision.allow()

        def check_sync(self, call: ToolCall) -> Decision:
            return Decision.allow()

        async def request_approval(self, call: ToolCall) -> Decision:
            return Decision.allow()

        def cancel_all(self) -> None: ...

    class MinLLM:
        @property
        def capabilities(self) -> ProviderCapabilities:
            return ProviderCapabilities()

        def stream(self, req: LLMRequest, signal: object) -> "AsyncIterator[StreamEvent]":  # type: ignore[override]
            ...

        async def count_tokens(self, req: LLMRequest) -> int:
            return 0

        async def aclose(self) -> None: ...

    class MinSecret:
        def get(self, key: str) -> str | None:
            return None

        def require(self, key: str) -> str:
            return ""

    assert isinstance(MinSink(), EventSink)
    assert isinstance(MinPerm(), PermissionPort)
    assert isinstance(MinLLM(), LLMProvider)
    assert isinstance(MinSecret(), SecretProvider)


# ── C9: no second definition ─────────────────────────────────────────────────
def test_c9_single_source_of_truth() -> None:
    assert av.ToolResult is tools_mod.ToolResult
    assert av.ToolCall is tools_mod.ToolCall
    assert av.Decision is tools_mod.Decision
    assert av.StreamEvent is events_mod.StreamEvent
    assert av.EventType is events_mod.EventType

    import autoverse as root

    assert root.ToolResult is tools_mod.ToolResult
    assert root.StreamEvent is events_mod.StreamEvent
    # all payloads frozen
    for payload_type in av.PAYLOAD_FOR.values():
        assert dataclasses.is_dataclass(payload_type)
        params = getattr(payload_type, "__dataclass_params__")
        assert params.frozen, f"{payload_type} must be frozen"
