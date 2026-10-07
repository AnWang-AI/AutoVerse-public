"""Unit tests for CompactionPolicy."""

from __future__ import annotations

from typing import Any

from autoverse.core.context.compaction import CompactionPolicy, CompactOutcome
from autoverse.core.context.config import CompactionConfig
from autoverse.types import EventType, Usage, UsagePayload
from autoverse.infra.persist.compaction_archive import InMemoryCompactionArchive
from autoverse.types.messages import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from tests._doubles import ScriptedProvider
from autoverse.types import TextDeltaPayload, TurnEndPayload, make_event
from autoverse.types import LLMRequest, ToolSchema


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, ev: Any) -> None:
        self.events.append(ev)

    def kinds(self) -> list[str]:
        return [
            e.payload.kind for e in self.events if e.type is EventType.COMPACTING
        ]


def _text(role: str, text: str) -> Message:
    return Message(role=role, content=(TextBlock(text=text),))  # type: ignore[arg-type]


def _noise(tid: str) -> tuple[Message, Message]:
    use = Message(role="assistant", content=(ToolUseBlock(id=tid, name="f", input={}),))  # type: ignore[arg-type]
    res = Message(role="user", content=(ToolResultBlock(tool_use_id=tid, content="x" * 300),))  # type: ignore[arg-type]
    return use, res


def _tiny_window_cfg(**kw: Any) -> CompactionConfig:
    return CompactionConfig(context_window=200, **kw)


def test_should_compact_threshold() -> None:
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(prov, _tiny_window_cfg())
    small = (_text("user", "x" * 30),)
    big = tuple(_text("user", "y" * 90) for _ in range(6))
    assert not pol.should_compact(small)
    assert pol.should_compact(big)


async def test_L14_micro_compact_folds_noise_keeps_tail() -> None:
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(context_window=400, micro_ratio=0.4, truncate_ratio=0.95,
                         micro_keep_tail=2),
    )
    u1, r1 = _noise("t1")
    u2, r2 = _noise("t2")
    msgs = (u1, r1, u2, r2, _text("user", "tail1"), _text("user", "tail2"))
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink)
    assert out.changed
    assert out.kind == "micro"
    assert "micro" in sink.kinds()
    # tail preserved
    assert _text("user", "tail2") in out.messages


async def test_micro_respects_cooldown() -> None:
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(context_window=2000, micro_ratio=0.2, truncate_ratio=0.95,
                         micro_keep_tail=4, cooldown_effective=1, cooldown_low=2),
    )
    u1, r1 = _noise("t1")
    u2, r2 = _noise("t2")
    u3, r3 = _noise("t3")
    # keep_tail=4 leaves t2/t3 noise in the tail; first pass folds t1, but the
    # cooldown must block a second pass even though foldable noise remains.
    msgs = (u1, r1, _text("user", "a" * 200), u2, r2, u3, r3, _text("user", "tail"))
    sink = _Sink()
    out1 = await pol.maybe_compact(msgs, sink)
    assert out1.changed
    assert out1.kind == "micro"
    out2 = await pol.maybe_compact(out1.messages + (u1, r1), sink)
    assert not out2.changed  # cooldown active


async def test_L15_graded_cooldown_by_reduction() -> None:
    # §4.7: an effective compaction (>= low_reduction_ratio) sets the short
    # cooldown; a low-reduction one sets a longer cooldown, and a repeated low
    # streak escalates to the 'stuck' cooldown.
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(cooldown_effective=1, cooldown_low=2, cooldown_stuck=5,
                         low_reduction_ratio=0.30),
    )
    # effective reduction -> short cooldown, low streak reset
    pol._grade_cooldown(before=1000, after=500)  # 50% reduction
    assert pol._calib.cooldown_target == 1
    assert pol._calib.consecutive_low == 0
    # low reduction -> longer cooldown
    pol._grade_cooldown(before=1000, after=900)  # 10%
    assert pol._calib.cooldown_target == 2
    assert pol._calib.consecutive_low == 1
    # repeated low -> stuck cooldown
    pol._grade_cooldown(before=1000, after=950)  # 5%
    assert pol._calib.cooldown_target == 5
    assert pol._calib.consecutive_low == 2


async def test_L15_emergency_truncate_over_hard_ceiling() -> None:
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2, truncate_ratio=0.5))
    msgs = tuple(_text("user", "z" * 90) for _ in range(8))
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink)
    assert out.changed
    assert out.kind == "truncate"
    assert "truncate" in sink.kinds()
    assert len(out.messages) <= 3


async def test_L16_force_compact_meta_summary() -> None:
    summary_turn = [
        make_event(TextDeltaPayload(text="A concise summary.")),
        make_event(UsagePayload(usage=Usage(input_tokens=50, output_tokens=10), model="m")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])
    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2))
    msgs = tuple(_text("user", f"m{i}" * 30) for i in range(6))
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed
    assert out.kind == "overflow"
    assert "A concise summary." in out.summary
    assert out.summary.startswith("[CONVERSATION SUMMARY]")
    assert "overflow" in sink.kinds()
    assert prov.requests[-1].temperature == 0.0
    summary_usage = [e for e in sink.events if e.type is EventType.USAGE]
    assert len(summary_usage) == 1
    assert summary_usage[0].payload.usage.total == 60


async def test_maybe_compact_escalates_to_meta_summary_when_no_noise() -> None:
    # No tool noise to micro-fold, estimate is in the [compact, truncate) band:
    # the cascade must escalate to an LLM meta-summary instead of doing nothing
    # (and instead of waiting for the hard-ceiling truncate).
    summary_turn = [
        make_event(TextDeltaPayload(text="Auto meta summary.")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(
            context_window=400,
            micro_ratio=0.4,
            compact_ratio=0.5,
            truncate_ratio=0.95,
            protected_tail=2,
            micro_keep_tail=2,
        ),
    )
    # est ~= 300 tokens -> in [compact=200, truncate=380): escalate to summary.
    msgs = tuple(_text("user", "m" * 150) for _ in range(6))
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink)
    assert out.changed
    assert out.kind == "overflow"
    assert "Auto meta summary." in out.summary
    assert "overflow" in sink.kinds()


async def test_maybe_compact_no_escalation_below_compact_threshold() -> None:
    # Over micro but below the meta threshold, with no foldable noise: no change
    # (do not summarise too eagerly).
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(
            context_window=400,
            micro_ratio=0.4,
            compact_ratio=0.95,
            truncate_ratio=0.99,
            protected_tail=2,
            micro_keep_tail=2,
        ),
    )
    # est ~= 300 tokens: over micro (160) but below the meta threshold (380).
    msgs = tuple(_text("user", "m" * 150) for _ in range(6))
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink)
    assert not out.changed
    assert sink.kinds() == []


async def test_force_compact_replaces_prior_summary_without_nesting() -> None:
    # A rolling summary consumes the previous generation but does not append it
    # verbatim to the replacement.
    summary_turn = [
        make_event(TextDeltaPayload(text="Fresh summary.")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])
    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2))
    prior = _text("system", "[CONVERSATION SUMMARY]\nOlder context.")
    body = tuple(_text("user", f"m{i}" * 30) for i in range(4))
    msgs = (prior,) + body
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed and out.kind == "overflow"
    sent = prov.requests[-1].messages
    assert prior in sent
    assert "Older context." not in out.summary
    assert out.summary.startswith("[CONVERSATION SUMMARY]")
    assert "[SUPERSEDED EARLIER SUMMARY]" not in out.summary


async def test_force_compact_summary_failure_falls_back_to_truncate() -> None:
    # provider raises -> summary fails -> overflow_truncate
    prov = ScriptedProvider([RuntimeError("llm down")])
    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2, summary_retries=0))
    msgs = tuple(_text("user", f"m{i}" * 30) for i in range(6))
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed
    assert out.kind == "overflow_truncate"
    assert "overflow_truncate" in sink.kinds()


def test_calibrate_ema_drifts() -> None:
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(prov, _tiny_window_cfg(ema_alpha=0.5))
    base = pol.estimate((_text("user", "x" * 30),))
    pol.calibrate(raw_estimate=10, actual_tokens=20)  # observed 2.0
    after = pol.estimate((_text("user", "x" * 30),))
    assert after > base


def test_summary_template_default_empty_uses_fallback() -> None:
    cfg = CompactionConfig()
    assert cfg.summary_template == ""


async def test_micro_archives_before_removal_and_emits_only_reference() -> None:
    archive = InMemoryCompactionArchive()
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(
            context_window=400,
            micro_ratio=0.4,
            truncate_ratio=0.95,
            micro_keep_tail=2,
        ),
        archive=archive,
        session_id="s1",
    )
    u1, r1 = _noise("t1")
    msgs = (u1, r1, _text("user", "x" * 300), _text("user", "tail"))
    sink = _Sink()
    out = await pol.maybe_compact(msgs, sink, turn_index=4)
    assert out.changed and out.archive_ref is not None
    assert out.archive_ref.turn_start == 0
    assert out.archive_ref.turn_end == 4
    page = await archive.read("s1", out.archive_ref.segment_id)
    assert "tool_result" in page.text
    event = next(e for e in sink.events if e.type is EventType.COMPACTING)
    assert event.payload.archive_segment_id == out.archive_ref.segment_id
    assert event.payload.folded == ()
    assert out.archive_ref.segment_id in out.messages[0].content[0].text  # type: ignore[union-attr]


async def test_emergency_truncates_single_oversized_protected_message() -> None:
    archive = InMemoryCompactionArchive()
    prov = ScriptedProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(context_window=100, truncate_ratio=0.5, protected_tail=6),
        archive=archive,
        session_id="s1",
    )
    msgs = (_text("user", "x" * 3_000),)
    out = await pol.maybe_compact(msgs, _Sink())
    assert out.changed
    assert out.tokens_after < out.tokens_before
    assert out.messages != msgs


async def test_complete_request_overhead_participates_in_budget() -> None:
    class _CountingProvider(ScriptedProvider):
        async def count_tokens(self, req: LLMRequest) -> int:
            return (
                len(req.system)
                + sum(len(str(t.json_schema)) for t in req.tools)
                + sum(len(getattr(b, "text", "")) for m in req.messages for b in m.content)
            ) // 4

    prov = _CountingProvider([[]])
    pol = CompactionPolicy(
        prov,
        CompactionConfig(context_window=1_000, micro_ratio=0.4, compact_ratio=0.5),
    )
    messages = (_text("user", "small"),)
    request = LLMRequest(
        messages=messages,
        system="s" * 2_000,
        tools=(ToolSchema("large", "d", {"description": "x" * 2_000}),),
        max_tokens=100,
    )
    out = await pol.maybe_compact(messages, _Sink(), request=request)
    assert not out.changed
    assert out.kind == "no_reduction", "immutable request overhead must be detected"
    assert out.tokens_before >= 1_000


# ── trace index integration (docs/architecture/runtime-flow.md §4.9) ────────────────────────────


async def test_force_compact_does_not_append_legacy_trace_block() -> None:
    """Legacy observability paths are not context-recovery pointers."""
    summary_turn = [
        make_event(TextDeltaPayload(text="A concise summary.")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])

    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2))
    msgs = tuple(_text("user", f"m{i}" * 30) for i in range(6))
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed
    assert out.kind == "overflow"
    assert "[Trace:" not in out.summary
    assert "/tmp/trace.jsonl" not in out.summary


async def test_force_compact_no_trace_block_when_callable_none() -> None:
    """No trace block when trace_index is None (default / graceful degradation)."""
    summary_turn = [
        make_event(TextDeltaPayload(text="A concise summary.")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])
    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2))
    msgs = tuple(_text("user", f"m{i}" * 30) for i in range(6))
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed
    assert "[Trace:" not in out.summary


async def test_force_compact_strips_and_ignores_legacy_trace_blocks() -> None:
    """Neither old nor newly offered trace paths survive compaction."""
    summary_turn = [
        make_event(TextDeltaPayload(text="Fresh summary.")),
        make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN")),
    ]
    prov = ScriptedProvider([summary_turn])

    pol = CompactionPolicy(prov, _tiny_window_cfg(protected_tail=2))
    prior = _text(
        "system",
        "[CONVERSATION SUMMARY]\nOlder context."
        "\n[Trace: session=test turns=0–3]\n→ /old.jsonl",
    )
    body = tuple(_text("user", f"m{i}" * 30) for i in range(4))
    msgs = (prior,) + body
    sink = _Sink()
    out = await pol.force_compact(msgs, sink)
    assert out.changed and out.kind == "overflow"
    # old block stripped
    assert "turns=0–3" not in out.summary
    assert "/old.jsonl" not in out.summary
    assert "[Trace: session=test turns=4–6]" not in out.summary
    assert "→ /tmp/trace.jsonl" not in out.summary
