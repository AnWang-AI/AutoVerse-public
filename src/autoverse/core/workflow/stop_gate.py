"""StopGate framework — continuation control (docs/reference/workflows.md §2).

A *gate* decides, after each assistant turn that produced no tool calls, whether
the loop should STOP, CONTINUE (with an optional injected user message), or hand
control back to the user (NEED_USER). Gates are registered in a
:class:`GateRegistry`; adding a new gate never requires touching the loop (P8).

``evaluate_all`` runs the selected gates in registration order with an
abort-priority short circuit: the first STOP (or NEED_USER) wins, but any gate
that itself wants to abort takes precedence. Every gate evaluation emits a
``GATE_DECISION`` event carrying a :class:`~autoverse.types.GatePayload` whose
``gate_name`` is the gate id and ``decision`` is the verdict value.

This module lives in ``core`` and imports only ``ports``/``types``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from autoverse.ports import EventSink
from autoverse.types.events import ErrorPayload, GatePayload, make_event
from autoverse.types.messages import Message


class Verdict(Enum):
    STOP = "stop"
    CONTINUE = "continue"
    NEED_USER = "need_user"


@dataclass(frozen=True)
class GateDecision:
    gate_id: str
    verdict: Verdict
    injected_message: Message | None = None
    reason: str = ""
    is_gate_error: bool = False  # set when the gate raised (fail-open, §4.3)


@runtime_checkable
class StopGate(Protocol):
    @property
    def id(self) -> str: ...

    async def evaluate(self, view: object, app: object) -> GateDecision: ...


class GateRegistry:
    """Ordered registry of gates; the loop only knows this façade (P8)."""

    def __init__(self) -> None:
        self._gates: list[StopGate] = []

    def register(self, gate: StopGate) -> None:
        self._gates.append(gate)

    def _select(self, gate_ids: tuple[str, ...]) -> list[StopGate]:
        if not gate_ids:
            return list(self._gates)
        by_id = {g.id: g for g in self._gates}
        return [by_id[gid] for gid in gate_ids if gid in by_id]

    async def evaluate_all(
        self,
        view: object,
        app: object,
        events: EventSink,
        gate_ids: tuple[str, ...] = (),
    ) -> GateDecision:
        """Run selected gates in order; the first objection short-circuits (§4.2).

        Per docs/reference/workflows.md §4.2/§4.3 the verdicts compose like this:

        * ``CONTINUE`` — a gate's criterion is not met: it carries feedback, the
          loop must rerun, and the remaining gates are skipped (first objection
          wins, saving the expensive Goal judge — §4.4).
        * ``NEED_USER`` — hand back to the user (also short-circuits).
        * ``STOP`` — "no objection, ask the next gate". Only when *every* gate
          abstains does the turn end, so the default (no gate / all STOP) is STOP.

        A gate that raises fails open as an *abstain* (``STOP`` with
        ``is_gate_error=True``) plus a structured ERROR event (phase="gate") — a
        broken quality gate must never block end_turn nor crash the loop (§4.3).
        Each consulted gate still emits a GATE_DECISION for observability.
        """
        last: GateDecision | None = None
        for gate in self._select(gate_ids):
            try:
                decision = await gate.evaluate(view, app)
            except Exception as exc:  # noqa: BLE001 — fail-open as abstain (§4.3)
                # A gate must never crash the loop: a failed gate degrades to an
                # abstain (STOP, do not block end_turn on a broken judge), emits a
                # structured ERROR with phase="gate", and is skipped.
                gate_id = getattr(gate, "id", "(unknown)")
                await events.emit(
                    make_event(
                        ErrorPayload(
                            message=f"gate {gate_id!r} failed: {exc}",
                            error_class="gate_error",
                            recoverable=True,
                            phase="gate",
                        )
                    )
                )
                last = GateDecision(
                    gate_id=gate_id,
                    verdict=Verdict.STOP,
                    reason=f"gate error: {exc}",
                    is_gate_error=True,
                )
                continue
            await events.emit(
                make_event(
                    GatePayload(
                        gate_name=decision.gate_id,
                        decision=decision.verdict.value,
                        reason=decision.reason,
                    )
                )
            )
            if decision.verdict is not Verdict.STOP:
                return decision  # CONTINUE / NEED_USER short-circuit (first objection)
            last = decision  # STOP = no objection → ask the next gate
        # All gates abstained (or none registered): end the turn.
        return last or GateDecision(gate_id="(default)", verdict=Verdict.STOP)


class CompletionGate:
    """Default gate: stop when there is no outstanding work (docs/reference/workflows.md §2.3).

    Reads ``AppContext.tasks``. When ``tasks`` is ``None`` (no task tracking)
    the assistant's no-tool turn is treated as completion -> STOP. A thrash
    guard escalates to NEED_USER when the same empty turn repeats.
    """

    id = "completion"

    def __init__(self, max_empty_repeats: int = 3) -> None:
        self._max_empty_repeats = max_empty_repeats
        self._empty_repeats = 0

    async def evaluate(self, view: object, app: object) -> GateDecision:
        tasks = getattr(app, "tasks", None)
        if tasks is None:
            return GateDecision(
                gate_id=self.id,
                verdict=Verdict.STOP,
                reason="no outstanding tasks",
            )
        self._empty_repeats += 1
        if self._empty_repeats >= self._max_empty_repeats:
            return GateDecision(
                gate_id=self.id,
                verdict=Verdict.NEED_USER,
                reason="repeated empty turns; handing back to user",
            )
        return GateDecision(
            gate_id=self.id,
            verdict=Verdict.CONTINUE,
            reason="tasks still pending",
        )


__all__ = [
    "Verdict",
    "GateDecision",
    "StopGate",
    "GateRegistry",
    "CompletionGate",
]
