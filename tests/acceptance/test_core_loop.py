"""Acceptance L1-L8 + L22-L24: the core TurnLoop end-to-end."""

from __future__ import annotations

import asyncio
from typing import Any

from autoverse.core.loop.types import StopCause, TurnOptions
from autoverse.core.workflow.stop_gate import GateRegistry, Verdict
from autoverse.types import (
    EventType,
    ProviderCapabilities,
    TextDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    Usage,
    UsagePayload,
    make_event,
)
from autoverse.types.messages import Message, TextBlock, ToolResultBlock
from autoverse.types.tools import ToolCall, ToolResult

import autoverse.core.tools.builtin  # noqa: F401 — trigger tool registration
from autoverse.core.tools.registry import ToolRegistry
from tests._doubles import FixedGate, ScriptedProvider
from tests.acceptance.conftest import make_loop


def _user(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


def _text_turn(text: str, stop: str = "END_TURN") -> list[Any]:
    return [
        make_event(TextDeltaPayload(text=text)),
        make_event(UsagePayload(usage=Usage(input_tokens=3, output_tokens=2), model="m")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause=stop)),
    ]


def _tool_turn(tid: str, name: str) -> list[Any]:
    return [
        make_event(ToolStartPayload(tool_use_id=tid, tool_name=name, tool_input={})),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
    ]


# ── L1: a plain text turn stops via the default CompletionGate (no tasks) ────
async def test_L1_simple_text_turn_ends() -> None:
    prov = ScriptedProvider([_text_turn("hello")])
    loop, conv, sink, _ = make_loop(prov)
    await conv.append(_user("hi"))
    result = await loop.run_turn()
    assert result.cause is StopCause.END_TURN
    assert isinstance(result.final_message, Message)
    assert result.final_message.content[0] == TextBlock(text="hello")


# ── L2: events are forwarded in order; TURN_START first, TURN_END last ───────
async def test_L2_turn_start_and_end_emitted() -> None:
    prov = ScriptedProvider([_text_turn("done")])
    loop, conv, sink, _ = make_loop(prov)
    await conv.append(_user("hi"))
    await loop.run_turn()
    assert sink.events[0].type is EventType.TURN_START
    assert sink.events[-1].type is EventType.TURN_END
    assert sink.events[-1].payload.stop_cause == "END_TURN"


# ── L3: a tool-call turn dispatches then continues to a text turn ────────────
async def test_L3_tool_call_then_continue() -> None:
    prov = ScriptedProvider([_tool_turn("t1", "bash"), _text_turn("after tool")])
    calls_seen: list[ToolCall] = []

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        calls_seen.extend(calls)
        return [ToolResult.ok("listing") for _ in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("run ls"))
    result = await loop.run_turn()
    assert result.cause is StopCause.END_TURN
    assert [c.name for c in calls_seen] == ["bash"]
    assert result.iterations == 2


# ── L4: tool results are appended as a user message with matching ids ────────
async def test_L4_tool_results_id_matched() -> None:
    prov = ScriptedProvider([_tool_turn("t1", "bash"), _text_turn("ok")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        return [ToolResult.ok("out-" + c.id) for c in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("go"))
    await loop.run_turn()
    msgs = conv.view().messages
    # find the tool-result message
    result_blocks = [
        b for m in msgs for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert len(result_blocks) == 1
    assert result_blocks[0].tool_use_id == "t1"
    assert result_blocks[0].content == "out-t1"


# ── L5: MAX_TURNS when the model never stops issuing tool calls ──────────────
async def test_L5_max_turns_ceiling() -> None:
    # provider always returns a tool turn -> loop never naturally ends
    prov = ScriptedProvider([_tool_turn("t", "bash")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        return [ToolResult.ok("x") for _ in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("loop forever"))
    result = await loop.run_turn(TurnOptions(max_iterations=3))
    assert result.cause is StopCause.MAX_TURNS
    assert result.iterations == 3


async def test_final_iteration_text_only_omits_tools() -> None:
    prov = ScriptedProvider([_tool_turn("t", "bash"), _text_turn("partial verdict")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        return [ToolResult.ok("x") for _ in calls]

    loop, conv, _, app = make_loop(prov, dispatch=dispatch)
    app.tools = ToolRegistry.build(include={"bash"})
    await conv.append(_user("loop once then summarize"))

    result = await loop.run_turn(
        TurnOptions(
            max_iterations=3,
            metadata={"final_iteration_text_only": True},
        )
    )

    assert result.cause is StopCause.END_TURN
    assert len(prov.requests) == 2
    assert [t.name for t in prov.requests[0].tools] == ["bash"]
    assert prov.requests[1].tools == ()
    assert "Do NOT call tools" in prov.requests[1].system


# ── L5 (docs/architecture/runtime-flow.md §3): cancel mid-turn -> ABORTED, reason=="cancel", no dangling tool_use
async def test_L5_cancel_aborts_with_reason_and_no_dangling_tool_use() -> None:
    prov = ScriptedProvider([_tool_turn("t", "bash")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        loop.abort("cancel")  # hard cancel after first dispatch
        return [ToolResult.ok("x") for _ in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("go"))
    result = await loop.run_turn(TurnOptions(max_iterations=10))
    assert result.cause is StopCause.ABORTED
    assert result.reason == "cancel"
    # No dangling tool_use: every tool_use in history has a matching tool_result.
    msgs = conv.view().messages
    tool_use_ids = {
        b.id
        for m in msgs
        for b in m.content
        if type(b).__name__ == "ToolUseBlock"
    }
    result_ids = {
        b.tool_use_id
        for m in msgs
        for b in m.content
        if type(b).__name__ == "ToolResultBlock"
    }
    assert tool_use_ids <= result_ids


# ── L6 (docs/architecture/runtime-flow.md §3): interrupt is a soft abort -> ABORTED, reason=="interrupt"
async def test_L6_interrupt_aborts_with_interrupt_reason() -> None:
    prov = ScriptedProvider([_tool_turn("t", "bash")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        loop.abort("interrupt")  # soft interrupt; tool backgrounding is dispatcher's job
        # the backgrounded tool still returns a (partial) result that is appended
        return [ToolResult.ok("partial output", interrupted=True) for _ in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("go"))
    result = await loop.run_turn(TurnOptions(max_iterations=10))
    assert result.cause is StopCause.ABORTED
    assert result.reason == "interrupt"  # distinct from cancel (L5)
    # partial output was folded back as a tool_result (feedback completeness P5)
    assert any(
        type(b).__name__ == "ToolResultBlock" and "partial output" in b.content
        for m in conv.view().messages
        for b in m.content
    )


async def test_cancel_wakes_stalled_llm_stream() -> None:
    """A cancel must stop a provider blocked before its next stream event."""

    class StalledProvider:
        async def stream(self, req: Any, signal: Any) -> Any:
            await asyncio.Event().wait()
            if False:
                yield None

    loop, conv, _, _ = make_loop(StalledProvider())
    await conv.append(_user("wait forever"))

    task = asyncio.create_task(loop.run_turn())
    await asyncio.sleep(0)
    loop.abort("cancel")
    result = await asyncio.wait_for(task, timeout=0.2)

    assert result.cause is StopCause.ABORTED
    assert result.reason == "cancel"


async def test_cancel_before_turn_task_starts_is_not_lost() -> None:
    """An abort after scheduling but before coroutine entry applies to that turn."""
    prov = ScriptedProvider([_text_turn("must not run")])
    loop, conv, _, _ = make_loop(prov)
    await conv.append(_user("race"))

    task = asyncio.create_task(loop.run_turn())
    loop.abort("cancel")
    result = await task

    assert result.cause is StopCause.ABORTED
    assert result.reason == "cancel"
    assert prov.requests == []

    # Completion rotates to a clean controller; the cancelled turn must not
    # poison the next user turn.
    result2 = await loop.run_turn()
    assert result2.cause is StopCause.END_TURN
    assert len(prov.requests) == 1


# ── L7: orphaned tool_use is repaired in the outgoing request ────────────────
async def test_L7_orphan_repair_in_request() -> None:
    from autoverse.types.messages import ToolUseBlock

    prov = ScriptedProvider([_text_turn("ok")])
    loop, conv, sink, _ = make_loop(prov)
    # seed conversation with an unanswered tool_use
    await conv.append(_user("go"))
    await conv.append(
        Message(role="assistant", content=(ToolUseBlock(id="orphan", name="f", input={}),))
    )
    await loop.run_turn()
    req = prov.requests[0]
    # the request must contain a synthetic tool_result for the orphan
    has_result = any(
        isinstance(b, ToolResultBlock) and b.tool_use_id == "orphan"
        for m in req.messages
        for b in m.content
    )
    assert has_result


# ── L8: a CONTINUE gate injects a user message and merges trailing users ─────
async def test_L8_continue_gate_injects_and_merges() -> None:
    # first turn: text (gate continues), second turn: text (gate stops)
    prov = ScriptedProvider([_text_turn("partial"), _text_turn("final")])
    gates = GateRegistry()
    gates.register(FixedGate("g_cont", Verdict.CONTINUE, reason="keep going"))
    state = {"calls": 0}

    class _OnceContinue:
        id = "once"

        async def evaluate(self, view: Any, app: Any) -> Any:
            from autoverse.core.workflow.stop_gate import GateDecision

            state["calls"] += 1
            if state["calls"] == 1:
                return GateDecision(
                    gate_id="once",
                    verdict=Verdict.CONTINUE,
                    injected_message=_user("continue please"),
                )
            return GateDecision(gate_id="once", verdict=Verdict.STOP)

    gates2 = GateRegistry()
    gates2.register(_OnceContinue())
    loop, conv, sink, _ = make_loop(prov, gates=gates2)
    await conv.append(_user("start"))
    result = await loop.run_turn()
    assert result.cause is StopCause.END_TURN
    assert result.iterations == 2
    # the injected user message must be present in the log
    texts = [
        b.text for m in conv.view().messages for b in m.content if isinstance(b, TextBlock)
    ]
    assert "continue please" in texts


# ── L22: usage accumulates across iterations ─────────────────────────────────
async def test_L22_usage_accumulates() -> None:
    prov = ScriptedProvider([_tool_turn("t1", "bash"), _text_turn("done")])

    async def dispatch(calls: list[ToolCall], ctx: Any) -> list[ToolResult]:
        return [ToolResult.ok("x") for _ in calls]

    loop, conv, sink, _ = make_loop(prov, dispatch=dispatch)
    await conv.append(_user("go"))
    result = await loop.run_turn()
    # tool turn has no usage event; text turn has 5 total -> total 5
    assert result.usage_total.total == 5


# ── L23: gate decisions are recorded on the result ───────────────────────────
async def test_L23_gate_decision_recorded() -> None:
    prov = ScriptedProvider([_text_turn("hello")])
    gates = GateRegistry()
    gates.register(FixedGate("only", Verdict.STOP, reason="we are done"))
    loop, conv, sink, _ = make_loop(prov, gates=gates)
    await conv.append(_user("hi"))
    result = await loop.run_turn()
    assert len(result.gate_decisions) == 1
    assert result.gate_decisions[0].gate_id == "only"
    assert result.gate_decisions[0].verdict is Verdict.STOP
    # GATE_DECISION event also emitted
    assert any(e.type is EventType.GATE_DECISION for e in sink.events)


# ── unrecoverable LLM error ends the turn with UNRECOVERABLE ─────────────────
async def test_unrecoverable_error() -> None:
    class _BadReq(Exception):
        http_status = 400
        error_kind = "bad_request"

    prov = ScriptedProvider([_BadReq()])
    loop, conv, sink, _ = make_loop(prov)
    await conv.append(_user("boom"))
    result = await loop.run_turn()
    assert result.cause is StopCause.UNRECOVERABLE
    assert any(e.type is EventType.ERROR for e in sink.events)


async def test_provider_retry_exhaustion_does_not_append_empty_success() -> None:
    from autoverse.infra.llm.taxonomy import ErrorKind, LLMError

    failure = LLMError(
        "HTTP 429: overloaded",
        http_status=429,
        platform_code="ServerOverloaded",
        kind=ErrorKind.RATE_LIMIT,
        retry_exhausted=True,
    )
    prov = ScriptedProvider([failure])
    loop, conv, sink, _ = make_loop(prov)
    await conv.append(_user("boom"))
    result = await loop.run_turn()
    assert result.cause is StopCause.RETRY_EXHAUSTED
    assert len(conv.view().messages) == 1
    errors = [event.payload for event in sink.events if event.type is EventType.ERROR]
    assert len(errors) == 1
    assert errors[0].error_class == "retryable"
    assert errors[0].platform_code == "ServerOverloaded"


# ── L24 (docs/architecture/runtime-flow.md §3): all gates STOP on plain text -> stop_cause == end_turn
async def test_L24_all_gates_stop_ends_turn() -> None:
    prov = ScriptedProvider([_text_turn("done")])
    gates = GateRegistry()
    gates.register(FixedGate("g1", Verdict.STOP))
    gates.register(FixedGate("g2", Verdict.STOP))
    loop, conv, sink, _ = make_loop(prov, gates=gates)
    await conv.append(_user("go"))
    result = await loop.run_turn()
    assert result.cause is StopCause.END_TURN
    assert result.iterations == 1


# ── mid-stream AbortError terminates as ABORTED, never triggers fallback ─────
async def test_mid_stream_abort_terminates_not_fallback() -> None:
    """Regression: an AbortError raised mid-stream was caught by the generic
    recovery path and classified as FALLBACK_MODEL (because AbortError has no
    http_status/error_kind), causing a model-fallback retry instead of
    terminating. A hard cancel must end the turn as ABORTED immediately."""
    from autoverse.types.abort import AbortError

    prov = ScriptedProvider([AbortError("cancel")])
    loop, conv, sink, _ = make_loop(prov)
    await conv.append(_user("hi"))
    result = await loop.run_turn()
    assert result.cause is StopCause.ABORTED
    # must NOT have retried / fallen back (single stream attempt)
    assert len(prov.requests) == 1


# ── COMPACT_RETRY is bounded: persistent context_overflow must not loop ──────
async def test_compact_retry_bounded_terminates() -> None:
    """Regression: COMPACT_RETRY did force_compact then retried without
    consuming any budget. A backend that keeps reporting context_overflow
    (and compaction that cannot reduce size) would loop forever. The loop
    must terminate as RETRY_EXHAUSTED."""
    from autoverse.core.context.compaction import CompactOutcome

    class _Overflow(Exception):
        error_kind = "context_overflow"

    class _NoProgressCompaction:
        def note_turn(self) -> None:
            pass

        async def maybe_compact(self, messages: Any, events: Any, **kwargs: Any) -> CompactOutcome:
            return CompactOutcome(changed=False)

        async def force_compact(self, messages: Any, events: Any, **kwargs: Any) -> CompactOutcome:
            # Reports "changed" but no token reduction -> non-progress.
            return CompactOutcome(
                changed=True, kind="overflow", messages=tuple(messages),
                tokens_before=100, tokens_after=100,
            )

        async def fits(self, messages: Any, request: Any) -> bool:
            return True

    prov = ScriptedProvider([_Overflow(), _Overflow(), _Overflow(), _Overflow()])
    loop, conv, sink, _ = make_loop(prov, compaction=_NoProgressCompaction())
    await conv.append(_user("boom"))
    result = await loop.run_turn()
    assert result.cause is StopCause.RETRY_EXHAUSTED
    assert any(e.type is EventType.ERROR for e in sink.events)


async def test_immutable_request_overhead_stops_before_provider_call() -> None:
    from autoverse.core.context.compaction import CompactionPolicy
    from autoverse.core.context.config import CompactionConfig

    provider = ScriptedProvider([_text_turn("must not run")])
    compaction = CompactionPolicy(
        provider,
        CompactionConfig(context_window=100, truncate_ratio=0.5),
    )
    loop, conversation, sink, _ = make_loop(provider, compaction=compaction)
    await conversation.append(_user("x" * 1_000))
    result = await loop.run_turn()
    assert result.cause is StopCause.UNRECOVERABLE
    assert provider.requests == []
    assert any(e.type is EventType.ERROR for e in sink.events)
