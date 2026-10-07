"""gap6 — TimelineBuilder flattens a TraceModel into ordered nodes (observability §4.5)."""

from __future__ import annotations

from autoverse.infra.observ.model import (
    CompactionSegment,
    LLMCallRecord,
    ToolCallRecord,
    TraceModel,
    TurnRecord,
)
from autoverse.infra.observ.model import CompactionKind
from autoverse.infra.observ.timeline import NodeKind, build_timeline


def _trace() -> TraceModel:
    t = TraceModel.new("s", "m")
    turn = TurnRecord(turn_index=0, model="m", start_mono=100.0, end_mono=110.0)
    turn.llm = LLMCallRecord(model="m", start_mono=100.0)
    # two tools, out of start order to verify the waterfall sort
    turn.tools = [
        ToolCallRecord(tool_use_id="b", name="grep", args={}, start_mono=105.0, end_mono=106.0),
        ToolCallRecord(tool_use_id="a", name="read_file", args={}, start_mono=101.0, end_mono=102.0),
    ]
    t.turns = [turn]
    t.compaction_segments = [
        CompactionSegment(kind=CompactionKind.MICRO, index=0, turn_start=0, turn_end=0)
    ]
    return t


def test_build_timeline_orders_nodes() -> None:
    nodes = build_timeline(_trace())
    kinds = [n.kind for n in nodes]
    assert NodeKind.TURN in kinds
    assert NodeKind.LLM in kinds
    assert NodeKind.TOOL in kinds
    assert NodeKind.COMPACTION in kinds


def test_tool_waterfall_sorted_by_start() -> None:
    nodes = build_timeline(_trace())
    tool_labels = [n.label for n in nodes if n.kind is NodeKind.TOOL]
    # read_file started at 101 (before grep at 105) → appears first
    assert tool_labels == ["read_file", "grep"]


def test_timeline_node_to_dict_roundtrips_shape() -> None:
    node = build_timeline(_trace())[0]
    d = node.to_dict()
    assert set(d) >= {"kind", "turn_index", "order", "label", "duration_ms"}


def test_empty_trace_yields_no_nodes() -> None:
    assert build_timeline(TraceModel.new("s", "m")) == []
