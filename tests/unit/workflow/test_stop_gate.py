"""Unit tests for the StopGate framework (docs/reference/workflows.md §2)."""

from __future__ import annotations

from typing import Any

from autoverse.core.workflow.stop_gate import (
    CompletionGate,
    GateDecision,
    GateRegistry,
    StopGate,
    Verdict,
)
from autoverse.types import EventType, GatePayload
from autoverse.types.messages import Message, TextBlock


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


class _Gate:
    def __init__(self, gate_id: str, verdict: Verdict) -> None:
        self._id = gate_id
        self._verdict = verdict

    @property
    def id(self) -> str:
        return self._id

    async def evaluate(self, view: object, app: object) -> GateDecision:
        return GateDecision(gate_id=self._id, verdict=self._verdict)


class _App:
    def __init__(self, tasks: object | None = None) -> None:
        self.tasks = tasks


def test_runtime_checkable_protocol() -> None:
    assert isinstance(CompletionGate(), StopGate)
    assert isinstance(_Gate("g", Verdict.STOP), StopGate)


async def test_completion_gate_stops_when_no_tasks() -> None:
    d = await CompletionGate().evaluate(None, _App(tasks=None))
    assert d.verdict is Verdict.STOP
    assert d.gate_id == "completion"


async def test_completion_gate_continues_then_thrash_need_user() -> None:
    gate = CompletionGate(max_empty_repeats=2)
    app = _App(tasks=["t1"])
    d1 = await gate.evaluate(None, app)
    assert d1.verdict is Verdict.CONTINUE
    d2 = await gate.evaluate(None, app)
    assert d2.verdict is Verdict.NEED_USER


async def test_registry_ordered_short_circuit() -> None:
    # STOP = "no objection, ask next"; the first CONTINUE (objection) short-circuits
    # (docs/reference/workflows.md §4.2). So an abstaining gate is consulted, then the objecting one
    # wins and the trailing gate never runs.
    reg = GateRegistry()
    reg.register(_Gate("a", Verdict.STOP))
    reg.register(_Gate("b", Verdict.CONTINUE))
    reg.register(_Gate("c", Verdict.STOP))
    sink = _Sink()
    decision = await reg.evaluate_all(None, None, sink)
    assert decision.gate_id == "b"
    assert decision.verdict is Verdict.CONTINUE
    # only a + b emitted; c short-circuited away by b's objection
    assert len(sink.events) == 2


async def test_registry_emits_gate_decision_payload() -> None:
    reg = GateRegistry()
    reg.register(_Gate("a", Verdict.STOP))
    sink = _Sink()
    await reg.evaluate_all(None, None, sink)
    ev = sink.events[0]
    assert ev.type is EventType.GATE_DECISION
    assert isinstance(ev.payload, GatePayload)
    assert ev.payload.gate_name == "a"
    assert ev.payload.decision == "stop"


async def test_registry_select_by_id() -> None:
    reg = GateRegistry()
    reg.register(_Gate("a", Verdict.STOP))
    reg.register(_Gate("b", Verdict.CONTINUE))
    sink = _Sink()
    decision = await reg.evaluate_all(None, None, sink, gate_ids=("b",))
    assert decision.gate_id == "b"
    assert decision.verdict is Verdict.CONTINUE


async def test_registry_default_stop_when_empty() -> None:
    # No gates / all-abstain → end the turn (default STOP), never an infinite
    # loop (docs/reference/workflows.md §4.2: "全部 STOP 才放行 end_turn").
    reg = GateRegistry()
    sink = _Sink()
    decision = await reg.evaluate_all(None, None, sink)
    assert decision.verdict is Verdict.STOP
