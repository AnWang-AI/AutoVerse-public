"""Phase 7 Workstream C acceptance — workflow W1–W7 (docs/reference/testing.md §14).

| W1 StopGate 同构注册     | W2 终止分层(机器优先)   | W3 goal 裁判熔断
| W4 pipeline 阶段门重试   | W5 自动批准下沉(异常安全)| W6 资源耗尽阀 RESOURCE_EXHAUSTED
| W7 新工作流 /fix 推演

Given/When/Then narrated in each test docstring. These exercise the public API
of ``autoverse.app.workflow`` against the *real* core ``GateRegistry`` to prove
the main loop is never edited to add a gate/workflow (P8).
"""

from __future__ import annotations

from typing import Any

import pytest

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.app.workflow import (
    Advance,
    AutoApprovalPolicy,
    BackgroundGate,
    CompletionGate,
    GoalEvaluator,
    GoalGate,
    GoalStore,
    JudgeResult,
    MachineVerdict,
    Orchestration,
    PhaseGate,
    PhaseResult,
    PhaseSpec,
    Pipeline,
    Termination,
    Trigger,
    WorkflowDefinition,
    WorkflowRegistry,
    auto_approval,
    fix_workflow,
)
from autoverse.core.loop.types import StopCause
from autoverse.core.workflow.stop_gate import GateRegistry, StopGate, Verdict
from autoverse.types.capability import CapabilityTier


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _App:
    """Minimal AppContext stand-in carrying the ports gates read."""

    def __init__(self, *, tasks: Any = None, jobs: Any = None) -> None:
        self.tasks = tasks
        self.jobs = jobs


class _Pending:
    """A tasks/jobs store double that always reports outstanding work."""

    def has_pending(self) -> bool:
        return True


class _Phase:
    name = "verify"

    def artifact_verified(self, view: Any) -> bool:
        return False

    @property
    def pending_reason(self) -> str:
        return "tests have not been run"


class _View:
    """A conversation-view double exposing an empty transcript."""

    messages: tuple[Any, ...] = ()


def fix_workflow_as_fixed_phases() -> WorkflowDefinition:
    """A FIXED_PHASES workflow so PhaseGate.applies_to is True (W1 setup)."""
    return WorkflowDefinition(
        name="phased",
        trigger=Trigger.USER_COMMAND,
        orchestration=Orchestration.FIXED_PHASES,
        advance=Advance.ARTIFACT_CHECK,
        termination=Termination.FIXED_PHASES_DONE,
        summary="a fixed-phase workflow",
    )


# ── W1 — StopGate isomorphic registration (the loop only iterates) ────────────
async def test_w1_four_gates_register_isomorphically() -> None:
    """Given completion/goal/background/phase gates,
    When registered into the *core* GateRegistry and iterated,
    Then they all conform to the same StopGate Protocol and the loop's
    evaluate_all consults every gate in registration order with no loop edit.

    Per docs/reference/workflows.md §4.2 a gate with no objection returns STOP ("ask the next
    gate") and the first CONTINUE short-circuits. So to prove the registry
    iterates *all four* in order, the first three abstain (no jobs / no tasks /
    no active phase) and only the last (Goal, judge "not yet") objects — every
    gate is consulted before the objecting one wins.
    """
    store = GoalStore()
    store.set("s1", "ship it")

    async def judge_not_yet(condition: str, transcript: Any) -> JudgeResult:
        return JudgeResult(ok=False, reason="not yet")

    wf = WorkflowRegistry()  # no active phased workflow → PhaseGate abstains

    gates = [
        BackgroundGate(session_id="s1"),
        CompletionGate(session_id="s1"),
        PhaseGate(registry=wf, session_id="s1", phase_provider=lambda: _Phase()),
        GoalGate(store=store, evaluator=GoalEvaluator(judge=judge_not_yet), session_id="s1"),
    ]
    # All four implement the *same* core StopGate Protocol (id + async evaluate).
    for g in gates:
        assert isinstance(g, StopGate)

    sink = _Sink()
    reg = GateRegistry()
    for g in gates:
        reg.register(g)
    app = _App(tasks=None, jobs=None)  # background + completion abstain
    decision = await reg.evaluate_all(_View(), app, sink)
    assert decision.verdict is Verdict.CONTINUE  # Goal objected (judge "not yet")
    assert decision.gate_id == "goal"
    assert decision.injected_message is not None
    feedback = " ".join(
        getattr(block, "text", "") for block in decision.injected_message.content
    )
    assert "ship it" in feedback
    assert "not yet" in feedback
    # Every registered gate emitted a GATE_DECISION, in registration order (P8).
    assert [e.payload.gate_name for e in sink.events] == [
        "background", "completion", "phase", "goal"
    ]


# ── W2 — termination layering: machine-verifiable consulted before judge ──────
async def test_w2_machine_layer_preferred_over_judge() -> None:
    """Given a goal with both a machine check and an LLM judge available,
    When the gate evaluates,
    Then the machine-verifiable layer is consulted first and the judge is skipped.
    """
    store = GoalStore()
    store.set("s1", "all tests pass")

    judge_calls: list[str] = []

    async def fake_judge(condition: str, transcript: Any) -> JudgeResult:
        judge_calls.append(condition)
        return JudgeResult(ok=False, reason="judge says not yet")

    def machine_check(view: Any, app: Any) -> MachineVerdict:
        return MachineVerdict(decided=True, ok=True, reason="pytest green")

    gate = GoalGate(
        store=store,
        evaluator=GoalEvaluator(judge=fake_judge),
        session_id="s1",
        machine_check=machine_check,
    )
    decision = await gate.evaluate(object(), _App())
    assert decision.verdict is Verdict.STOP        # machine layer decided "done"
    assert judge_calls == []                        # judge never consulted (W2)
    assert store.get("s1") is None
    assert store.last_achieved("s1") is not None
    assert store.last_achieved("s1").achieved


# ── W3 — goal judge breaker distinguishes "not achieved" from "judge fault" ───
async def test_w3_judge_failure_trips_breaker_distinct_from_not_achieved() -> None:
    """Given a declarative goal,
    When the judge raises repeatedly (malfunction) vs returns ok=False,
    Then a malfunction trips the circuit breaker and clears the goal, while a
    plain not-achieved verdict keeps looping and resets the breaker count.
    """
    store = GoalStore()
    store.set("s1", "make it work")

    # 1) Plain "not achieved" → CONTINUE, breaker count stays 0.
    async def judge_not_yet(condition: str, transcript: Any) -> JudgeResult:
        return JudgeResult(ok=False, reason="still failing")

    sink = _Sink()
    gate_ok = GoalGate(
        store=store, evaluator=GoalEvaluator(judge=judge_not_yet),
        session_id="s1", events=sink,
    )
    d = await gate_ok.evaluate(object(), _App())
    assert d.verdict is Verdict.CONTINUE
    assert store.get("s1").consecutive_judge_errors == 0  # reset on normal verdict

    # 2) Judge malfunction (raises) three times → breaker trips, goal cleared.
    async def judge_boom(condition: str, transcript: Any) -> JudgeResult:
        raise RuntimeError("judge 5xx")

    sink2 = _Sink()
    gate_err = GoalGate(
        store=store, evaluator=GoalEvaluator(judge=judge_boom),
        session_id="s1", events=sink2,
    )
    for _ in range(3):
        d = await gate_err.evaluate(object(), _App())
        assert d.verdict is Verdict.STOP        # judge fault never fed back as negative
        assert d.reason and "judge" in d.reason.lower()
    # Breaker tripped → goal auto-cleared and an ERROR event surfaced to the user.
    assert store.get("s1") is None
    assert any(getattr(e, "type", None) is not None for e in sink2.events)


# ── W4 — pipeline phase gate: fail once, retry, then pass ─────────────────────
async def test_w4_pipeline_retry_loop() -> None:
    """Given an architect→builder→reviewer pipeline,
    When the reviewer gate fails on the first attempt then passes,
    Then the pipeline retries the failing phase and finally completes.
    """
    attempts = {"reviewer": 0}

    class Runner:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            if spec.name == "reviewer":
                attempts["reviewer"] += 1
                passed = attempts["reviewer"] >= 2     # fail first, pass second
                score = 9 if passed else 3
                return PhaseResult(spec.name, f"review#{attempts['reviewer']}", passed, score)
            return PhaseResult(spec.name, f"{spec.name}-out", True)

    pipe = Pipeline(runner=Runner(), events=_Sink())
    phases = [
        PhaseSpec("architect", "architect", "plan it"),
        PhaseSpec("builder", "builder", "build {prev}"),
        PhaseSpec("reviewer", "reviewer", "review {prev}", score_threshold=7),
    ]
    outcome = await pipe.run(phases, max_retries=2)
    assert outcome.cause is StopCause.END_TURN
    assert attempts["reviewer"] == 2                    # retried exactly once
    assert outcome.results[-1].passed


# ── W5 — auto-approval sink-down: capability ceiling + exception-safe ─────────
async def test_w5_auto_approval_respects_ceiling_and_is_exception_safe() -> None:
    """Given a pipeline auto-approving controlled writes,
    When a write within the ceiling and a destructive op above it are tested,
    Then the in-ceiling op is auto-approved, the over-ceiling op is not, and the
    policy is restored even if the with-body raises (no dangling auto-approve).
    """
    policy = AutoApprovalPolicy()           # default: nothing auto-approved
    assert not policy.allows(CapabilityTier.CONTROLLED_WRITE)

    # Auto-approval sinks down to CONTROLLED_WRITE for the duration of the block.
    try:
        with auto_approval(policy, ceiling=CapabilityTier.CONTROLLED_WRITE):
            assert policy.allows(CapabilityTier.READ_ONLY)
            assert policy.allows(CapabilityTier.CONTROLLED_WRITE)
            assert not policy.allows(CapabilityTier.ARBITRARY_WRITE)   # over ceiling
            assert not policy.allows(CapabilityTier.PROCESS)
            raise RuntimeError("phase blew up mid-flight")
    except RuntimeError:
        pass
    # Exception-safe: policy restored to "nothing auto-approved".
    assert not policy.allows(CapabilityTier.CONTROLLED_WRITE)


# ── W6 — resource-exhaustion valve forces RESOURCE_EXHAUSTED ──────────────────
async def test_w6_resource_exhausted_terminates() -> None:
    """Given a pipeline whose review never reaches threshold (loop risk),
    When retries are exhausted,
    Then the pipeline terminates with RESOURCE_EXHAUSTED rather than running away.
    """
    class NeverPasses:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            if spec.name == "reviewer":
                return PhaseResult(spec.name, "still bad", False, score=2)
            return PhaseResult(spec.name, f"{spec.name}-out", True)

    pipe = Pipeline(runner=NeverPasses(), events=_Sink())
    phases = [
        PhaseSpec("builder", "builder", "build"),
        PhaseSpec("reviewer", "reviewer", "review", score_threshold=7),
    ]
    outcome = await pipe.run(phases, max_retries=2)
    assert outcome.cause is StopCause.RESOURCE_EXHAUSTED
    assert "reviewer" in (outcome.reason or "")


# ── W7 — derive + register a new /fix workflow and run its phases ─────────────
async def test_w7_fix_workflow_registers_and_runs_phases() -> None:
    """Given the four-dimension definition of /fix,
    When it is registered into the WorkflowRegistry and dispatched,
    Then it runs its phases in order without any change to the core loop.
    """
    reg = WorkflowRegistry()
    defn = fix_workflow()
    reg.register(defn)

    got = reg.get("fix")
    assert got is not None
    assert got.trigger is Trigger.USER_COMMAND
    assert got.orchestration is Orchestration.DYNAMIC_LOOP
    assert got.advance is Advance.ARTIFACT_CHECK
    assert Termination.MACHINE_VERIFIABLE in got.termination
    assert got.phases is not None and len(got.phases) >= 2

    ran: list[str] = []

    class Runner:
        async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult:
            ran.append(spec.name)
            return PhaseResult(spec.name, f"{spec.name}-done", True)

    pipe = Pipeline(runner=Runner(), events=_Sink())
    outcome = await pipe.run(list(got.phases), max_retries=0)
    assert outcome.cause is StopCause.END_TURN
    assert ran == [p.name for p in got.phases]          # phases ran in order


async def test_engine_builder_run_workflow_is_wired(tmp_path) -> None:
    """EngineBuilder exposes registered workflows through EngineAdapter."""
    engine = await EngineBuilder(EngineOptions(model="echo", cwd=tmp_path)).build()
    out = await engine.run_workflow("init", "scan conventions")
    assert out.startswith("## ✓ /init completed")
    assert "end_turn" not in out
    assert "unavailable" not in out


async def test_goal_survives_cold_resume_and_clear_removes_it(tmp_path) -> None:
    from autoverse.infra.persist.jsonl_store import JSONLStore

    store = JSONLStore(tmp_path / "sessions")
    sid = "goal-resume"
    first = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_id=sid, store=store)
    ).build()
    first.app.goal.set(sid, "ship only when tests pass")
    await first.app.goal.persist(sid)

    resumed = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_id=sid, store=store)
    ).build()
    assert resumed.app.goal.get(sid) is not None
    assert resumed.app.goal.get(sid).condition == "ship only when tests pass"

    await resumed.clear_in_place()
    cold = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_id=sid, store=store)
    ).build()
    assert cold.app.goal.get(sid) is None


# ── /dream runner invocation (KDR-11: runner-based workflow wiring) ──────────
async def test_dream_runner_invoked_via_engine(tmp_path) -> None:
    """Given an engine whose trace contains recurring tool sequences,
    When /dream is invoked via ``run_workflow``,
    Then the runner is actually called (not "no executable phases") and facts
    are consolidated into project memory.
    """
    from autoverse.infra.observ.model import TurnRecord, ToolCallRecord

    engine = await EngineBuilder(EngineOptions(model="echo", cwd=tmp_path)).build()
    trace = engine.app.trace
    assert trace is not None, "trace collector should be wired by default"
    # Populate three turns with the same 2-tool sequence (≥ min_occurrences=2).
    for i in range(3):
        turn = TurnRecord(turn_index=i, model="echo", start_mono=0.0)
        turn.tools.append(ToolCallRecord(tool_use_id=f"t{i}a", name="read_file", args={}))
        turn.tools.append(ToolCallRecord(tool_use_id=f"t{i}b", name="edit_file", args={}))
        trace.model.turns.append(turn)

    out = await engine.run_workflow("dream")
    assert "[dream]" in out
    assert "consolidated" in out
    assert "no executable phases" not in out
    # The memory sink should have written to MEMORY.md.
    assert (tmp_path / "MEMORY.md").exists()


async def test_distill_runner_invoked_via_engine(tmp_path) -> None:
    """Given an engine whose trace has a sequence repeated ≥ threshold,
    When /distill is invoked via ``run_workflow``,
    Then the runner packages it as a distilled skill (not "no executable phases").
    """
    from autoverse.infra.observ.model import TurnRecord, ToolCallRecord

    engine = await EngineBuilder(EngineOptions(model="echo", cwd=tmp_path)).build()
    trace = engine.app.trace
    assert trace is not None
    # threshold=3 → repeat the same 2-tool sequence 4 times.
    for i in range(4):
        turn = TurnRecord(turn_index=i, model="echo", start_mono=0.0)
        turn.tools.append(ToolCallRecord(tool_use_id=f"t{i}a", name="read_file", args={}))
        turn.tools.append(ToolCallRecord(tool_use_id=f"t{i}b", name="grep", args={}))
        trace.model.turns.append(turn)

    out = await engine.run_workflow("distill")
    assert "[distill]" in out
    assert "packaged" in out
    assert "no executable phases" not in out


async def test_dream_degrades_gracefully_without_trace(tmp_path) -> None:
    """Given an engine built with trace disabled,
    When /dream is invoked,
    Then it degrades gracefully with an 'unavailable' message (never raises).
    """
    from autoverse.app.config import Config

    config = Config().model_copy(update={"trace_enabled": False})
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, settings=config)
    ).build()
    out = await engine.run_workflow("dream")
    assert "unavailable" in out
    assert "no trace collector" in out
