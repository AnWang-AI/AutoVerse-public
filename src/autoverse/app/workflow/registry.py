"""WorkflowRegistry + the four-dimension WorkflowDefinition (docs/reference/workflows.md §3/§8).

A *workflow* is the orchestration unit that "constrains the LLM's non-determinism
with deterministic stop criteria until it converges on a verifiable artifact"
(§3.1). Every workflow — present or future — decomposes into four orthogonal
dimensions (§3.2): **trigger**, **orchestration**, **advance**, **termination**.
These enums *are* the schema; deriving a new workflow is "filling the table"
(§11), and registering it never touches ``TurnLoop`` or the gate iteration (P8).

The registry is the single source of truth (P1): ``/help`` and ``/status`` derive
from it rather than keeping a parallel hand-maintained list. There is no global
singleton (P4) — the instance is held by ``AppContext`` / injected by the builder.

This module lives in ``app`` and may import ``core``/``infra`` (import-linter
only forbids the reverse). It deliberately imports nothing heavy: the
``PhaseSpec`` / ``StopGate`` references are type-only to avoid cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # type-only — excluded from import-linter, avoids cycles
    from autoverse.app.workflow.pipeline import PhaseSpec
    from autoverse.core.workflow.stop_gate import StopGate


# ── Dimension 1 · Trigger — who starts it (§3.2) ──────────────────────────────
class Trigger(Enum):
    USER_COMMAND = "user_command"      # explicit slash command (/goal, /coordinate)
    LLM_INVOKED = "llm_invoked"        # the main LLM dispatches it (agent / Skill tool)
    AUTO_CONDITION = "auto_condition"  # runtime condition (token threshold, /watch timer)
    LIFECYCLE_EVENT = "lifecycle_event"  # hook (PreToolUse / Stop)


# ── Dimension 2 · Orchestration — how phases are organised (§3.2) ─────────────
class Orchestration(Enum):
    SINGLE_TURN = "single_turn"            # done in one turn (/plan)
    FIXED_PHASES = "fixed_phases"          # ordered phases advanced one by one (/execute)
    DYNAMIC_LOOP = "dynamic_loop"          # loop until criterion met (/goal, completion)
    MULTI_AGENT_DAG = "multi_agent_dag"    # sub-agents on a dependency graph (/coordinate)


# ── Dimension 3 · Advance — what triggers the next phase (§3.2) ───────────────
class Advance(Enum):
    USER_APPROVAL = "user_approval"    # human in the loop (strong)
    ARTIFACT_CHECK = "artifact_check"  # phase artifact verified (strong, mechanism)
    TOOL_EVENT = "tool_event"          # a tool was called, e.g. ExitPlanMode (strong)
    SCORE_THRESHOLD = "score_threshold"  # evaluator score >= threshold (strong)
    PROMPT_ONLY = "prompt_only"        # prompt-only — weak, never the sole signal


# ── Dimension 4 · Termination — what counts as "done" (§3.2, ↓ determinism) ───
class Termination(Enum):
    MACHINE_VERIFIABLE = "machine_verifiable"  # tests/coverage/build/lint (highest)
    SCORE_THRESHOLD = "score_threshold"        # judge score >= threshold
    EXTERNAL_JUDGE = "external_judge"          # independent judge binary verdict
    FIXED_PHASES_DONE = "fixed_phases_done"    # all fixed phases completed
    USER_APPROVAL = "user_approval"            # user confirmation
    MODEL_SELF_CLAIM = "model_self_claim"      # the model declares done (lowest)
    RESOURCE_EXHAUSTED = "resource_exhausted"  # turns/tokens/wall-clock — the safety valve


#: Default per-session judge-fault circuit-breaker ceiling (§6.4 / §5.2).
MAX_CONSECUTIVE_JUDGE_ERRORS = 3


@dataclass(frozen=True)
class WorkflowDefinition:
    """Declarative four-dimension description of one workflow (§8.1).

    ``termination`` is normalised to a tuple so composite criteria (§5.3, e.g.
    ``MACHINE_VERIFIABLE AND SCORE_THRESHOLD``) and single criteria share one
    shape. The orchestration-specific implementation is supplied via exactly one
    of ``runner`` / ``phases`` / ``gate`` (§8.3 dispatch).
    """

    name: str
    trigger: Trigger
    orchestration: Orchestration
    advance: Advance
    termination: tuple[Termination, ...]
    summary: str
    # Orchestration implementation (pick one per §8.3 dispatch):
    runner: Callable[..., object] | None = None       # MULTI_AGENT_DAG → Pipeline method
    phases: "tuple[PhaseSpec, ...] | None" = None      # FIXED_PHASES / phased loop
    gate: "StopGate | None" = None                     # DYNAMIC_LOOP → stop gate
    # Universal safety valve (cannot be switched off, §5.2):
    max_turns: int | None = None        # None → inherit AppContext.config.max_turns
    max_wall_seconds: float = 1800.0
    needs_wrapup: bool = True           # graded wrap-up (§10); SINGLE_TURN sets False

    def __post_init__(self) -> None:
        if isinstance(self.termination, Termination):
            object.__setattr__(self, "termination", (self.termination,))
        else:
            object.__setattr__(self, "termination", tuple(self.termination))
        if isinstance(self.phases, list):
            object.__setattr__(self, "phases", tuple(self.phases))


class WorkflowRegistry:
    """Single source of truth for workflow definitions + per-session activation.

    No global singleton (P4): the engine builder constructs one and injects it.
    """

    def __init__(self) -> None:
        self._defs: dict[str, WorkflowDefinition] = {}
        self._active: dict[str, str] = {}   # session_id -> workflow name

    def register(self, defn: WorkflowDefinition) -> None:
        """Register a definition; a name clash is an error, never a silent overwrite (P8)."""
        if defn.name in self._defs:
            raise ValueError(f"workflow already registered: {defn.name!r}")
        self._defs[defn.name] = defn

    def register_decorated(self) -> None:
        """Drain the ``@workflow`` decorator buffer into this registry (P8)."""
        for defn in drain_pending():
            self.register(defn)

    def get(self, name: str) -> WorkflowDefinition | None:
        return self._defs.get(name)

    def all(self) -> list[WorkflowDefinition]:
        return list(self._defs.values())

    # ── per-session activation (P4: state lives here, not in a singleton) ────
    def set_active(self, session_id: str, name: str) -> None:
        if name not in self._defs:
            raise KeyError(f"unknown workflow: {name!r}")
        self._active[session_id] = name

    def active(self, session_id: str) -> WorkflowDefinition | None:
        name = self._active.get(session_id)
        return self._defs.get(name) if name is not None else None

    def clear_active(self, session_id: str) -> None:
        self._active.pop(session_id, None)


# ── @workflow decorator (P8: new workflow = register, no engine edit) ─────────
_PENDING: list[WorkflowDefinition] = []


def workflow(
    *,
    name: str,
    trigger: Trigger,
    orchestration: Orchestration,
    advance: Advance,
    termination: Termination | tuple[Termination, ...],
    summary: str,
    phases: "tuple[PhaseSpec, ...] | None" = None,
    gate: "StopGate | None" = None,
    max_turns: int | None = None,
    max_wall_seconds: float = 1800.0,
    needs_wrapup: bool = True,
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    """Decorate a runner callable; queue its ``WorkflowDefinition`` for registration.

    The builder calls ``WorkflowRegistry.register_decorated()`` at assembly time
    so adding a workflow never edits the engine (P8).
    """

    term = (termination,) if isinstance(termination, Termination) else tuple(termination)

    def deco(fn: Callable[..., object]) -> Callable[..., object]:
        _PENDING.append(
            WorkflowDefinition(
                name=name,
                trigger=trigger,
                orchestration=orchestration,
                advance=advance,
                termination=term,
                summary=summary,
                runner=fn,
                phases=phases,
                gate=gate,
                max_turns=max_turns,
                max_wall_seconds=max_wall_seconds,
                needs_wrapup=needs_wrapup,
            )
        )
        return fn

    return deco


def drain_pending() -> list[WorkflowDefinition]:
    """Return and clear the pending decorator buffer."""
    out = list(_PENDING)
    _PENDING.clear()
    return out


__all__ = [
    "Trigger",
    "Orchestration",
    "Advance",
    "Termination",
    "MAX_CONSECUTIVE_JUDGE_ERRORS",
    "WorkflowDefinition",
    "WorkflowRegistry",
    "workflow",
    "drain_pending",
]
