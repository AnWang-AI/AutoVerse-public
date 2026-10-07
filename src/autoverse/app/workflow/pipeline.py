"""Pipeline — MULTI_AGENT_DAG orchestration with phase gates (docs/reference/workflows.md §7).

A :class:`Pipeline` runs a task as ordered phases, each handled by a specialised
sub-agent (architect / builder / reviewer / validator …), advancing via phase
gates (``ARTIFACT_CHECK`` / ``SCORE_THRESHOLD``). It does **not** implement the
sub-agents themselves — it dispatches them through an injected ``PhaseRunner``
(in production a thin shell over ``SubAgentRunner.spawn``); capability tiers,
isolation, and post-audit are the sub-agent subsystem's job (P2: orchestration ≠
isolation).

Three behaviours mandated by docs/reference/workflows.md §4:

* **W4** — a failing phase gate triggers rollback/retry of that phase (with the
  failure fed back as context), and the scoring retry loop in :meth:`coordinate`
  works.
* **W5** — :func:`auto_approval` sinks auto-approval down to a per-session
  :class:`CapabilityTier` ceiling and is exception-safe (restored on any exit).
* **W6** — when retries are exhausted the pipeline terminates with
  ``StopCause.RESOURCE_EXHAUSTED`` rather than running away.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable

from autoverse.core.loop.types import StopCause
from autoverse.ports import EventSink
from autoverse.types.capability import CapabilityTier


@dataclass(frozen=True)
class PhaseSpec:
    """One pipeline phase (a DAG node). ``readonly`` is a node attribute (§7.2)."""

    name: str
    agent_type: str
    task_template: str                 # may reference the previous output via {prev}
    readonly: bool = False             # hard-isolate to read-only (e.g. explore)
    score_threshold: int | None = None  # SCORE_THRESHOLD phases: pass iff score >= this


@dataclass(frozen=True)
class PhaseResult:
    """Outcome of one phase dispatch (§7.2)."""

    phase: str
    output: str
    passed: bool                       # did the phase gate let it advance?
    score: int | None = None           # SCORE_THRESHOLD phases
    halt: bool = False                 # early stop (e.g. "nothing to commit", §7.4)


@dataclass(frozen=True)
class PipelineOutcome:
    """Aggregate result. ``cause`` is END_TURN on success, RESOURCE_EXHAUSTED on valve."""

    results: tuple[PhaseResult, ...]
    cause: StopCause
    reason: str = ""


@runtime_checkable
class PhaseRunner(Protocol):
    """Injected dispatcher for one phase (DI, P3/P4)."""

    async def run_phase(self, spec: PhaseSpec, prev: str) -> PhaseResult: ...


def _phase_passed(spec: PhaseSpec, res: PhaseResult) -> bool:
    """A phase advances iff its gate passes. SCORE_THRESHOLD overrides ``passed``."""
    if spec.score_threshold is not None:
        return res.score is not None and res.score >= spec.score_threshold
    return res.passed


class Pipeline:
    def __init__(self, *, runner: PhaseRunner, events: EventSink) -> None:
        self._runner = runner
        # Kept in the constructor for API compatibility and future workflow-level
        # events. Individual phase lifecycle belongs to SubAgentRunner, which
        # already emits one complete running → terminal card per dispatch.
        self._events = events

    async def _dispatch(self, spec: PhaseSpec, prev: str) -> PhaseResult:
        return await self._runner.run_phase(spec, prev)

    async def run(self, phases: list[PhaseSpec], *, max_retries: int = 0) -> PipelineOutcome:
        """Run phases in order; a failing phase gate retries that phase (W4).

        Retries carry the failure context implicitly via the threaded ``prev``.
        When a phase still fails after ``max_retries`` retries, terminate with
        ``RESOURCE_EXHAUSTED`` (W6) — never loop unbounded.
        """
        results: list[PhaseResult] = []
        prev = ""
        for spec in phases:
            attempt = 0
            while True:
                res = await self._dispatch(spec, prev)
                results.append(res)
                if res.halt:                          # early stop (§7.4 commit pipeline)
                    return PipelineOutcome(tuple(results), StopCause.END_TURN, "halted early")
                if _phase_passed(spec, res):
                    prev = res.output
                    break
                attempt += 1
                if attempt > max_retries:             # safety valve (W6)
                    return PipelineOutcome(
                        tuple(results),
                        StopCause.RESOURCE_EXHAUSTED,
                        f"phase {spec.name!r} did not pass after {max_retries} retries",
                    )
                prev = res.output                     # feed failure back into the retry
        return PipelineOutcome(tuple(results), StopCause.END_TURN)

    async def coordinate(
        self, task: str, *, threshold: int = 7, max_retries: int = 3
    ) -> PipelineOutcome:
        """Architect → (builder → evaluator) scoring retry loop (§7.5).

        Breaks when the evaluator score reaches ``threshold``; exits with
        ``RESOURCE_EXHAUSTED`` when retries are spent (W6).
        """
        results: list[PhaseResult] = []
        plan = await self._dispatch(PhaseSpec("architect", "architect", task), task)
        results.append(plan)
        feedback = ""
        prev = plan.output
        for attempt in range(max_retries + 1):
            build = await self._dispatch(
                PhaseSpec("builder", "builder", "build {prev}"), f"{prev}\n{feedback}".strip()
            )
            review = await self._dispatch(
                PhaseSpec("evaluator", "reviewer", "review {prev}", score_threshold=threshold),
                build.output,
            )
            results.extend((build, review))
            if review.score is not None and review.score >= threshold:
                return PipelineOutcome(tuple(results), StopCause.END_TURN)
            feedback = review.output                  # carry review into the next build
            prev = build.output
        return PipelineOutcome(
            tuple(results),
            StopCause.RESOURCE_EXHAUSTED,
            f"score below {threshold} after {max_retries} retries",
        )


# ── Auto-approval sink-down (W5 / §8.4) ───────────────────────────────────────
class AutoApprovalPolicy:
    """Per-session auto-approval state. Default: nothing is auto-approved.

    The ceiling is the highest :class:`CapabilityTier` that may be auto-approved
    without a user prompt; anything above it (irreversible / high hidden-cost
    operations) still goes through ``PermissionPort.check`` (P7). There is no
    global holder — one instance lives per session in ``AppContext.workflow``.
    """

    def __init__(self) -> None:
        self._ceiling: CapabilityTier | None = None

    @property
    def ceiling(self) -> CapabilityTier | None:
        return self._ceiling

    def allows(self, tier: CapabilityTier) -> bool:
        return self._ceiling is not None and tier <= self._ceiling

    def _set(self, ceiling: CapabilityTier | None) -> None:
        self._ceiling = ceiling


@contextmanager
def auto_approval(policy: AutoApprovalPolicy, *, ceiling: CapabilityTier) -> Iterator[None]:
    """Sink auto-approval down to ``ceiling`` for the block, restoring on exit.

    Exception-safe (W5 / §8.4): the previous ceiling is restored even if the body
    raises, so no "global auto-approve" state is ever left dangling.
    """
    previous = policy.ceiling
    policy._set(ceiling)
    try:
        yield
    finally:
        policy._set(previous)


__all__ = [
    "PhaseSpec",
    "PhaseResult",
    "PipelineOutcome",
    "PhaseRunner",
    "Pipeline",
    "AutoApprovalPolicy",
    "auto_approval",
]
