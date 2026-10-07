"""Turn-loop value types (docs/architecture/runtime-flow.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from autoverse.core.workflow.stop_gate import GateDecision
from autoverse.types.messages import Message, Usage


class StopCause(Enum):
    END_TURN = "end_turn"          # gate said STOP / model finished naturally
    MAX_TURNS = "max_turns"        # hit the iteration ceiling
    ABORTED = "aborted"            # abort signal fired
    UNRECOVERABLE = "unrecoverable"  # error classified as terminal
    RETRY_EXHAUSTED = "retry_exhausted"  # ran out of retries/fallbacks
    RESOURCE_EXHAUSTED = "resource_exhausted"  # workflow safety valve fired (docs/reference/workflows.md §5.2/§7)
    PLAN_PROPOSED = "plan_proposed"  # ExitPlanMode submitted a plan; wait for user approval


@dataclass(frozen=True)
class TurnOptions:
    max_iterations: int = 100
    allow_compaction: bool = True
    gate_ids: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class TurnResult:
    final_message: Message | None
    cause: StopCause
    iterations: int = 0
    usage_total: Usage = field(default_factory=Usage)
    gate_decisions: tuple[GateDecision, ...] = ()
    # When cause is ABORTED, the abort reason ("interrupt" | "cancel") so callers
    # can distinguish a soft interrupt from a hard cancel (docs/architecture/runtime-flow.md §3 L5).
    reason: str | None = None


__all__ = ["StopCause", "TurnOptions", "TurnResult"]
