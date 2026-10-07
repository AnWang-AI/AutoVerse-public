"""autoverse.app.workflow — workflow orchestration layer (docs/reference/workflows.md).

The single source of truth for workflow definitions (four dimensions), concrete
StopGates, declarative goals, multi-phase pipelines, and the /dream + /distill
self-improvement closure. Adding a workflow or a gate happens here via
registration only — the core ``TurnLoop`` is never edited (P8).
"""

from __future__ import annotations

from autoverse.app.workflow.gates import (
    BackgroundGate,
    CompletionGate,
    CurrentPhase,
    GoalGate,
    MachineCheck,
    MachineVerdict,
    PhaseGate,
    PhaseProvider,
)
from autoverse.app.workflow.goal import (
    CLEAR_ALIASES,
    MAX_CONSECUTIVE_JUDGE_ERRORS,
    MAX_CONDITION_LENGTH,
    GoalEntry,
    GoalEvaluator,
    GoalStore,
    Judge,
    JudgeFn,
    JudgeResult,
)
from autoverse.app.workflow.pipeline import (
    AutoApprovalPolicy,
    PhaseResult,
    PhaseRunner,
    PhaseSpec,
    Pipeline,
    PipelineOutcome,
    auto_approval,
)
from autoverse.app.workflow.registry import (
    Advance,
    Orchestration,
    Termination,
    Trigger,
    WorkflowDefinition,
    WorkflowRegistry,
    drain_pending,
    workflow,
)
from autoverse.app.workflow.self_improve import (
    AssetRegistry,
    DistillResult,
    DreamResult,
    MemorySink,
    TraceSource,
    distill,
    distill_workflow,
    dream,
    dream_workflow,
    fix_workflow,
)

__all__ = [
    # registry / four dimensions
    "Advance",
    "Orchestration",
    "Termination",
    "Trigger",
    "WorkflowDefinition",
    "WorkflowRegistry",
    "workflow",
    "drain_pending",
    "MAX_CONSECUTIVE_JUDGE_ERRORS",
    "MAX_CONDITION_LENGTH",
    "CLEAR_ALIASES",
    # goal
    "GoalEntry",
    "GoalStore",
    "GoalEvaluator",
    "Judge",
    "JudgeFn",
    "JudgeResult",
    # gates
    "BackgroundGate",
    "CompletionGate",
    "PhaseGate",
    "CurrentPhase",
    "PhaseProvider",
    "GoalGate",
    "MachineVerdict",
    "MachineCheck",
    # pipeline
    "PhaseSpec",
    "PhaseResult",
    "PhaseRunner",
    "PipelineOutcome",
    "Pipeline",
    "AutoApprovalPolicy",
    "auto_approval",
    # self-improvement
    "TraceSource",
    "MemorySink",
    "AssetRegistry",
    "DreamResult",
    "DistillResult",
    "dream",
    "distill",
    "dream_workflow",
    "distill_workflow",
    "fix_workflow",
]
