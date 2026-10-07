"""Concrete StopGates: Completion / Goal / Background / Phase (docs/reference/workflows.md §4).

All four implement the *core* ``StopGate`` Protocol structurally (``id`` +
``async evaluate(view, app)``) yet live in ``app`` — which is fine because the
contract is structural and ``core`` never imports ``app`` (import-linter only
forbids the reverse). They register into the core ``GateRegistry`` isomorphically;
adding any of them never touches ``TurnLoop`` (P8, W1).

The doc's ``applies_to`` notion (§4.4) maps onto the actual two-verdict protocol
like this: a gate that does **not** apply to the current workflow simply returns
``Verdict.STOP`` ("no objection — ask the next gate"), so a normal conversation
that activates no workflow incurs zero judge calls / phase state.

Determinism layering (§5/§6.3, W2): :class:`GoalGate` consults an optional
*machine-verifiable* check **before** the LLM judge. If the machine layer decides,
the judge is never called — "if it can be a test, prefer the test".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from autoverse.app.workflow.goal import GoalEvaluator, GoalStore
from autoverse.app.workflow.registry import Orchestration, WorkflowRegistry
from autoverse.core.workflow.stop_gate import GateDecision, Verdict
from autoverse.ports import EventSink
from autoverse.types.events import ErrorPayload, make_event
from autoverse.types.messages import Message, TextBlock


def _user(text: str) -> Message:
    return Message(role="user", content=(TextBlock(text=text),))


# ── BackgroundGate — collect parallel sub-agent results first (order 10) ──────
class BackgroundGate:
    """Stop is blocked while background jobs are still uncollected (§4.4).

    Reads ``app.jobs`` (usually ``InMemoryJobStore`` on the default runtime
    path). A job store exposing ``has_pending()`` that returns True → CONTINUE
    so the next gate sees the full context; otherwise (or no job store) → STOP
    (no objection).
    """

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id

    @property
    def id(self) -> str:
        return "background"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        jobs = getattr(app, "jobs", None)
        pending = getattr(jobs, "has_pending", None)
        is_pending = False
        if callable(pending):
            try:
                is_pending = bool(pending(self._session_id))
            except TypeError:
                is_pending = bool(pending())
        if is_pending:
            return GateDecision(
                gate_id=self.id,
                verdict=Verdict.CONTINUE,
                injected_message=_user(
                    "[SYSTEM] Background sub-agents are still running; collect their "
                    "results before finishing."
                ),
                reason="background jobs pending",
            )
        return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no pending jobs")


# ── CompletionGate — explicit todo state (order 20) ───────────────────────────
class CompletionGate:
    """Stop only when no task is still pending (§4.4 / §6.3 ARTIFACT_CHECK).

    Reads ``app.tasks`` (a TaskStore). ``None`` (no task tracking) → STOP.
    A ``has_pending()`` that returns True → CONTINUE.
    """

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id

    @property
    def id(self) -> str:
        return "completion"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        tasks = getattr(app, "tasks", None)
        if tasks is None:
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no task tracking")
        pending = getattr(tasks, "has_pending", None)
        if callable(pending) and bool(pending()):
            return GateDecision(
                gate_id=self.id,
                verdict=Verdict.CONTINUE,
                injected_message=_user(
                    "[SYSTEM] There are still pending todo items; complete them before finishing."
                ),
                reason="tasks still pending",
            )
        return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no outstanding tasks")


# ── PhaseGate — fixed-phase artifact verification (order 30) ──────────────────
@runtime_checkable
class CurrentPhase(Protocol):
    """The current phase the gate verifies (duck-typed, supplied per-session)."""

    name: str

    def artifact_verified(self, view: object) -> bool: ...

    @property
    def pending_reason(self) -> str: ...


#: A per-session current-phase provider. ``None`` → no phase to verify.
PhaseProvider = Callable[[], "CurrentPhase | None"]


class PhaseGate:
    """Active only for ``FIXED_PHASES`` workflows; verifies the current phase artifact.

    For any other (or no) active workflow → STOP, so it costs nothing on a normal
    conversation (§4.4). This upgrades the advance signal from PROMPT_ONLY (weak)
    to ARTIFACT_CHECK (strong) — it stops "the model skipped VERIFY and declared
    done" (§7.3).

    The immutable :class:`WorkflowDefinition` does not carry mutable progress, so
    the current phase is supplied by an injected ``phase_provider`` (a per-session
    tracker), keeping definition (declarative) and progress (mutable) separate.
    """

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        session_id: str,
        phase_provider: PhaseProvider | None = None,
    ) -> None:
        self._registry = registry
        self._session_id = session_id
        self._phase_provider = phase_provider

    @property
    def id(self) -> str:
        return "phase"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        defn = self._registry.active(self._session_id)
        if defn is None or defn.orchestration is not Orchestration.FIXED_PHASES:
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="not a phased workflow")
        phase = self._phase_provider() if self._phase_provider is not None else None
        # No phase tracker wired → no objection (a Pipeline drives phases instead).
        if phase is None:
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no active phase")
        verify = getattr(phase, "artifact_verified", None)
        if callable(verify) and bool(verify(view)):
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="phase artifact verified")
        reason = getattr(phase, "pending_reason", "the phase artifact is not yet verified")
        name = getattr(phase, "name", "current")
        return GateDecision(
            gate_id=self.id,
            verdict=Verdict.CONTINUE,
            injected_message=_user(
                f"[SYSTEM] You are still in the {name} phase; {reason}. Finish it before ending."
            ),
            reason=f"phase {name} not verified",
        )


@dataclass(frozen=True)
class MachineVerdict:
    """Result of a machine-verifiable check (§5.1 highest determinism layer)."""

    decided: bool       # True iff the machine layer can conclude (no judge needed)
    ok: bool            # when decided: condition satisfied?
    reason: str = ""


#: A machine check: (view, app) -> MachineVerdict. Reads test/lint/build state.
MachineCheck = Callable[[object, object], MachineVerdict]


# ── GoalGate — declarative judge with machine-layer preference (order 40) ─────
class GoalGate:
    """Active only when the session has a declarative goal (§6 / §4.4).

    Termination layering (W2 / §6.3): if a ``machine_check`` is supplied and it
    *decides*, that verdict wins and the (expensive) LLM judge is never called.
    Only when the machine layer is inconclusive does it fall back to the judge.

    Judge-fault handling (W3 / §6.4): a malfunction is recorded via the breaker
    and returned as ``STOP`` (never as negative feedback). A tripped breaker
    clears the goal and surfaces an ERROR event to the user.
    """

    def __init__(
        self,
        *,
        store: GoalStore,
        evaluator: GoalEvaluator | None,
        session_id: str,
        machine_check: MachineCheck | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._store = store
        self._evaluator = evaluator
        self._session_id = session_id
        self._machine_check = machine_check
        self._events = events

    @property
    def id(self) -> str:
        return "goal"

    async def evaluate(self, view: object, app: object) -> GateDecision:
        entry = self._store.get(self._session_id)
        if entry is None:
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no active goal")

        # Layer 1 — machine-verifiable check preferred over the judge (W2/§6.3).
        if self._machine_check is not None:
            mv = self._machine_check(view, app)
            if mv.decided:
                self._store.record_judgement(self._session_id, is_judge_error=False)
                if mv.ok:
                    self._store.mark_achieved(self._session_id, mv.reason)
                    await self._store.persist(self._session_id)
                    return GateDecision(
                        gate_id=self.id, verdict=Verdict.STOP,
                        reason=mv.reason or "machine check satisfied",
                    )
                await self._store.persist(self._session_id)
                return GateDecision(
                    gate_id=self.id, verdict=Verdict.CONTINUE,
                    injected_message=_user(
                        "[Goal evaluation — machine check not met]\n"
                        f"Condition: {entry.condition}\n"
                        f"Machine feedback: {mv.reason or 'Condition not yet met'}\n\n"
                        "Please continue working toward this exact goal."
                    ),
                    reason=mv.reason or "machine check failed",
                )

        # Layer 2 — LLM judge.
        if self._evaluator is None:
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="no evaluator")
        transcript = list(getattr(view, "messages", ()) or ())
        result = await self._evaluator.evaluate(entry.condition, transcript)
        entry.turns_evaluated += 1
        entry.tokens_spent += result.tokens_used

        if result.is_judge_error:
            tripped = self._store.record_judgement(self._session_id, is_judge_error=True)
            await self._store.persist(self._session_id)
            if tripped and self._events is not None:
                await self._events.emit(
                    make_event(
                        ErrorPayload(
                            message=(
                                "The goal judge has been unavailable repeatedly; the goal "
                                "was auto-cancelled. Please retry later."
                            ),
                            error_class="GoalJudgeBreaker",
                            recoverable=False,
                            phase="gate",
                        )
                    )
                )
            # Judge fault must NOT be fed back as negative feedback (P5) → STOP.
            return GateDecision(
                gate_id=self.id, verdict=Verdict.STOP,
                reason=f"judge error: {result.reason}",
            )

        # Normal verdict resets the breaker.
        self._store.record_judgement(self._session_id, is_judge_error=False)
        if result.ok:
            self._store.mark_achieved(self._session_id, result.reason)
            await self._store.persist(self._session_id)
            return GateDecision(gate_id=self.id, verdict=Verdict.STOP, reason="goal achieved")
        entry.last_reason = result.reason
        await self._store.persist(self._session_id)
        return GateDecision(
            gate_id=self.id,
            verdict=Verdict.CONTINUE,
            injected_message=_user(
                "[Goal evaluation — condition not met]\n"
                f"Condition: {entry.condition}\n"
                f"Evaluator feedback: {result.reason or 'Condition not yet met'}\n\n"
                "Please continue working toward this exact goal and address the "
                "evaluator feedback above."
            ),
            reason=result.reason or "goal not achieved",
        )


__all__ = [
    "BackgroundGate",
    "CompletionGate",
    "PhaseGate",
    "CurrentPhase",
    "PhaseProvider",
    "GoalGate",
    "MachineVerdict",
    "MachineCheck",
]
