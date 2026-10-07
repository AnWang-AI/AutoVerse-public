"""gap5 — StopGate fail-open (docs/reference/workflows.md §4.3).

A gate that raises must not crash the loop: evaluate_all degrades that gate to an
*abstain* (STOP, "本轮放行"), emits a structured ERROR (phase='gate'), sets
is_gate_error, and keeps evaluating subsequent gates. Fail-open here means "do not
block end_turn on a broken quality gate" — the verdict is STOP, not CONTINUE.
"""

from __future__ import annotations

from typing import Any

from autoverse.core.workflow.stop_gate import GateDecision, GateRegistry, Verdict
from autoverse.types import EventType


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)


class _BoomGate:
    id = "boom"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        raise RuntimeError("judge exploded")


class _StopGate:
    id = "stopper"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        return GateDecision(gate_id="stopper", verdict=Verdict.STOP)


async def test_failing_gate_fails_open_and_emits_error() -> None:
    reg = GateRegistry()
    reg.register(_BoomGate())
    reg.register(_StopGate())
    sink = _Sink()
    decision = await reg.evaluate_all(object(), object(), sink)
    # the boom gate degraded to CONTINUE; the stopper gate still ran -> STOP
    assert decision.verdict is Verdict.STOP
    errors = [e for e in sink.events if e.type is EventType.ERROR]
    assert errors, "expected an ERROR event from the failing gate"
    assert errors[0].payload.phase == "gate"


async def test_failing_gate_alone_abstains() -> None:
    reg = GateRegistry()
    reg.register(_BoomGate())
    sink = _Sink()
    decision = await reg.evaluate_all(object(), object(), sink)
    # fail-open = abstain (STOP), never crash and never block end_turn (§4.3)
    assert decision.verdict is Verdict.STOP
    assert decision.is_gate_error is True
    errors = [e for e in sink.events if e.type is EventType.ERROR]
    assert errors, "expected an ERROR event from the failing gate"
