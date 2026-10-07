"""TimelineBuilder — flatten a TraceModel into an ordered node list (docs/reference/events-and-storage.md §4.5).

Reconstruction (O3) is more than a round-trip: a *timeline* linearises turns,
tool calls (in start order — the parallel-tool waterfall), retries, errors, gate
decisions, and compaction segments into one ordered sequence a viewer/export can
render top-to-bottom. Compaction is a first-class node ordered by its turn span.

Pure / read-only over the trace model — no engine reflection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from autoverse.infra.observ.model import TraceModel


class NodeKind(Enum):
    TURN = "turn"
    LLM = "llm"
    TOOL = "tool"
    RETRY = "retry"
    ERROR = "error"
    GATE = "gate"
    COMPACTION = "compaction"


@dataclass(frozen=True)
class TimelineNode:
    kind: NodeKind
    turn_index: int
    order: float          # sort key within the timeline (monotonic where known)
    label: str
    detail: str = ""
    duration_ms: float = 0.0
    is_error: bool = False
    data: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "turn_index": self.turn_index,
            "order": self.order,
            "label": self.label,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "is_error": self.is_error,
            "data": dict(self.data),
        }


def build_timeline(trace: TraceModel) -> list[TimelineNode]:
    """Linearise ``trace`` into ordered :class:`TimelineNode`s (observability §4.5)."""
    nodes: list[TimelineNode] = []
    for turn in trace.turns:
        ti = turn.turn_index
        nodes.append(
            TimelineNode(
                kind=NodeKind.TURN, turn_index=ti, order=turn.start_mono,
                label=f"turn {ti}", detail=turn.model,
                duration_ms=turn.total_duration_ms,
            )
        )
        if turn.llm is not None:
            nodes.append(
                TimelineNode(
                    kind=NodeKind.LLM, turn_index=ti, order=turn.start_mono + 1e-6,
                    label=f"llm {turn.model}",
                )
            )
        # tool calls in start order — exposes the parallel-tool waterfall.
        for tool in sorted(turn.tools, key=lambda t: t.start_mono):
            nodes.append(
                TimelineNode(
                    kind=NodeKind.TOOL, turn_index=ti, order=tool.start_mono,
                    label=tool.name, detail=tool.output_preview,
                    duration_ms=tool.duration_ms, is_error=tool.is_error,
                    data={"tool_use_id": tool.tool_use_id, "decision": tool.decision.value},
                )
            )
        for retry in turn.retries:
            nodes.append(
                TimelineNode(
                    kind=NodeKind.RETRY, turn_index=ti, order=turn.start_mono + 2e-6,
                    label=f"retry #{retry.attempt}", detail=retry.reason,
                )
            )
        for err in turn.errors:
            nodes.append(
                TimelineNode(
                    kind=NodeKind.ERROR, turn_index=ti, order=turn.start_mono + 3e-6,
                    label="error", detail=err.message, is_error=True,
                )
            )
        if turn.gate is not None:
            nodes.append(
                TimelineNode(
                    kind=NodeKind.GATE, turn_index=ti,
                    order=(turn.end_mono or turn.start_mono) + 4e-6,
                    label=f"gate {turn.gate.gate_name}", detail=turn.gate.decision,
                )
            )
    # compaction segments ordered by their turn span (first-class nodes).
    for seg in trace.compaction_segments:
        nodes.append(
            TimelineNode(
                kind=NodeKind.COMPACTION, turn_index=seg.turn_end,
                order=float(seg.turn_end) + 5e-6,
                label=f"compaction:{seg.kind.value}",
                detail=f"turns {seg.turn_start}-{seg.turn_end}",
                data={"index": seg.index, "truncated": seg.truncated},
            )
        )
    nodes.sort(key=lambda n: (n.turn_index, n.order))
    return nodes


__all__ = ["NodeKind", "TimelineNode", "build_timeline"]
