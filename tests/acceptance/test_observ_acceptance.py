"""Phase 5 acceptance — observability O1–O16 (docs/reference/testing.md §9).

The collector is driven purely by hand-built StreamEvents (O1: reconstructs the
trace from StreamEvent only, no engine reference).

| O1 collector==EventSink | O2 metering | O3 replay/reconstruct | O4 export
| O5 viewer data API (external assets) | O6 parent/child link | O7 redaction
| O8 FanOut isolation | O9 request snapshot | O10 system dedup | O11 lossless
| O12 system redaction | O13 retry record | O14 error record
| O15 denied tool visible | O16 structured tool failure
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autoverse.infra.observ.collector import TraceCollector
from autoverse.infra.observ.export import export_html, export_markdown
from autoverse.infra.observ.fanout import FanOutSink
from autoverse.infra.observ.meter import Meter, ModelPricing
from autoverse.infra.observ.model import (
    CompactionKind,
    ErrorKind,
    LLMCallRecord,
    Phase,
    ToolDecision,
    TraceModel,
    TurnRecord,
)
from autoverse.infra.observ.redact import REDACTED, Redactor
from autoverse.infra.observ.store import TraceStore
from autoverse.infra.observ.viewer.server import TraceViewer, static_dir
from autoverse.ports import EventSink
from autoverse.types.events import (
    ApprovalPayload,
    CompactingPayload,
    ErrorPayload,
    GatePayload,
    RequestPayload,
    RetryPayload,
    StreamEvent,
    SubagentActivityPayload,
    TextDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
    make_event,
)
from autoverse.types.messages import Message, TextBlock, ToolResultBlock, Usage


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


async def _drive(collector: TraceCollector, events: list[StreamEvent]) -> None:
    for ev in events:
        await collector.emit(ev)


def _request_event(turn: int, system: str, *, tools: tuple[str, ...] = ("read",)) -> StreamEvent:
    return make_event(
        RequestPayload(
            turn_index=turn,
            system=system,
            system_sha256=_sha(system),
            tool_names=tools,
            tools_sha256=_sha(",".join(tools)),
            model="m1",
            max_tokens=1000,
            temperature=0.5,
            thinking_budget=0,
            active_skills=("plan",) if turn == 0 else ("plan", "review"),
        )
    )


# ── O1 — TraceCollector is an EventSink, reconstructs from StreamEvent only ───
async def test_o1_collector_is_event_sink(tmp_path: Path) -> None:
    """Given a TraceCollector
    When emitting a session's events
    Then it is a structural EventSink and rebuilds the trace from events only."""
    collector = TraceCollector("s1", "m1")
    assert isinstance(collector, EventSink)

    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(TextDeltaPayload(text="hi")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    assert len(collector.model.turns) == 1
    assert collector.model.turns[0].llm is not None
    assert collector.model.turns[0].llm.text_preview == "hi"


def test_resumed_collector_seeds_meter_from_persisted_turns() -> None:
    model = TraceModel.new("resume", "m1", wall=1.0)
    turn = TurnRecord(turn_index=0, model="m1", start_mono=1.0, end_mono=2.0)
    turn.llm = LLMCallRecord(
        model="m1",
        start_mono=1.0,
        end_mono=2.0,
        usage=Usage(input_tokens=100, output_tokens=20, cache_read_tokens=50),
    )
    model.turns.append(turn)

    collector = TraceCollector("resume", "m1", initial_model=model)

    assert collector.meter.total_usage() == Usage(
        input_tokens=100, output_tokens=20, cache_read_tokens=50
    )


# ── O2 — metering: tokens + cost aggregated per model pricing ─────────────────
async def test_o2_metering_cost(tmp_path: Path) -> None:
    """Given multiple USAGE events across models
    When aggregated
    Then input/output/cache tokens and cost roll up by model pricing."""
    pricing = {
        "m1": ModelPricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
        "m2": ModelPricing(input=2.0, output=10.0),
    }
    collector = TraceCollector("s2", "m1", meter=Meter(pricing))
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(UsagePayload(usage=Usage(input_tokens=1_000_000, output_tokens=500_000), model="m1")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
            make_event(TurnStartPayload(turn_index=1, iteration=0, model="m2")),
            make_event(UsagePayload(usage=Usage(input_tokens=2_000_000), model="m2")),
            make_event(TurnEndPayload(turn_index=1, iterations=1, stop_cause="end_turn")),
        ],
    )
    meter = collector.meter
    assert meter.usage_by_model()["m1"].input_tokens == 1_000_000
    cost = meter.total_cost()
    # m1: 1M*3 + 0.5M*15 = 3 + 7.5 = 10.5 ; m2: 2M*2 = 4 → 14.5
    assert round(cost.total_usd, 2) == 14.5
    # unknown model → unknown cost (no fabricated price)
    assert meter.cost_for("ghost", Usage(input_tokens=10)).known is False


# ── O3 — replay: turn boundaries, tool IO, compaction/clear reconstructed ─────
async def test_o3_replay_reconstruct(tmp_path: Path) -> None:
    """Given a persisted trace
    When reloaded
    Then turn boundaries, tool input/output and compaction/clear are restored."""
    store = TraceStore(tmp_path)
    collector = TraceCollector("s3", "m1", store)
    folded = (Message(role="user", content=(TextBlock(text="old chat"),)),)
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(ToolStartPayload(tool_use_id="t1", tool_name="bash", tool_input={"command": "ls"})),
            make_event(ToolEndPayload(tool_use_id="t1", tool_name="bash", output="a.txt")),
            make_event(CompactingPayload(kind="clear", summary="cleared", folded=folded)),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    await collector.aclose()

    reloaded = await store.load("s3")
    assert reloaded is not None
    assert len(reloaded.turns) == 1
    tool = reloaded.turns[0].tools[0]
    assert tool.name == "bash"
    assert tool.args == {"command": "ls"}
    assert tool.output_preview == "a.txt"
    assert reloaded.compaction_segments[0].kind is CompactionKind.CLEAR
    assert reloaded.compaction_segments[0].messages[0]["role"] == "user"


# ── O4 — export self-contained HTML/Markdown ──────────────────────────────────
async def test_o4_export_self_contained(tmp_path: Path) -> None:
    """Given a trace
    When exported to HTML/Markdown
    Then each is a single offline-openable file with no external assets."""
    collector = TraceCollector("s4", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(UsagePayload(usage=Usage(input_tokens=100, output_tokens=50), model="m1")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    html = export_html(collector.model)
    assert "<html" in html and "</html>" in html
    assert "http://" not in html and "https://" not in html  # no external links
    assert "<script id=\"trace-data\"" in html  # inlined data

    md = export_markdown(collector.model)
    assert "# Trace" in md and "Turn 0" in md


# ── O5 — viewer data API; SPA assets external (not embedded) ──────────────────
async def test_o5_viewer_data_api_external_assets(tmp_path: Path) -> None:
    """Given a TraceViewer over a TraceStore
    When GET /sessions and /session/{id}
    Then structured data is returned; SPA assets live as external files."""
    store = TraceStore(tmp_path)
    collector = TraceCollector("sV", "m1", store)
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    await collector.aclose()

    viewer = TraceViewer(store)
    sessions = await viewer.sessions()
    assert any(s["session_id"] == "sV" for s in sessions)
    detail = await viewer.session("sV")
    assert detail is not None and detail["session_id"] == "sV"

    # SPA assets are external files on disk, not Python literals.
    assert (static_dir() / "index.html").is_file()
    assert (static_dir() / "app.js").is_file()
    status, ctype, body = await viewer.handle("/static/app.js")
    assert status == 200 and b"fetch" in body
    server_src = Path(viewer.__class__.__module__.replace(".", "/"))
    src_text = (Path("src") / server_src.with_suffix(".py")).read_text()
    assert "<html" not in src_text  # server holds no embedded HTML


# ── O6 — parent/child trace link via SUBAGENT_ACTIVITY ────────────────────────
async def test_o6_parent_child_link(tmp_path: Path) -> None:
    """Given a parent engine spawning a sub-agent
    When it receives SUBAGENT_ACTIVITY
    Then the activity is merged onto the parent trace with the child session id."""
    collector = TraceCollector("parent", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(
                SubagentActivityPayload(
                    job_id="job-1",
                    subagent_type="builder",
                    activity="progress",
                    detail="read 3 files",
                    child_session_id="child-1",
                )
            ),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    acts = collector.model.subagent_activity
    assert acts and acts[0]["child_session_id"] == "child-1"
    assert acts[0]["subagent_type"] == "builder"


# ── O7 — redaction: secrets never enter the trace ─────────────────────────────
async def test_o7_redaction_tool_args(tmp_path: Path) -> None:
    """Given a request/tool carrying a secret
    When folded
    Then the secret value never lands in the trace."""
    secret = "sk-supersecretvalue123"
    collector = TraceCollector("s7", "m1", redactor=Redactor([secret]))
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(
                ToolStartPayload(
                    tool_use_id="t1",
                    tool_name="web_fetch",
                    tool_input={"headers": f"Authorization: Bearer {secret}"},
                )
            ),
            make_event(ToolEndPayload(tool_use_id="t1", tool_name="web_fetch", output="ok")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    args = collector.model.turns[0].tools[0].args
    assert secret not in str(args)
    assert REDACTED in str(args)


async def test_compaction_reference_is_incremental_and_legacy_folded_is_redacted(
    tmp_path: Path,
) -> None:
    secret = "sk-folded-secret-value123"
    store = TraceStore(tmp_path)
    collector = TraceCollector("compact-safe", "m1", store, redactor=Redactor([secret]))
    folded = (
        Message(
            role="user",
            content=(ToolResultBlock(tool_use_id="t1", content=secret),),
        ),
    )
    await collector.emit(
        make_event(
            CompactingPayload(
                kind="micro",
                archive_segment_id="seg-123",
                folded_count=1,
                folded_sha256="abc",
                folded=folded,
            )
        )
    )

    rebuilt = await store.rebuild_from_jsonl("compact-safe")
    assert rebuilt is not None
    assert rebuilt.compaction_segments[0].archive_segment_id == "seg-123"
    assert secret not in (tmp_path / "compact-safe" / "trace.jsonl").read_text()
    assert REDACTED in str(rebuilt.compaction_segments[0].messages)


# ── O8 — FanOut: one emit broadcasts to all sinks; one failing is isolated ────
async def test_o8_fanout_isolation(tmp_path: Path) -> None:
    """Given renderer + trace + a failing sink
    When emitting once
    Then all sinks receive it and one raising does not break the others."""

    class _Boom:
        async def emit(self, event: StreamEvent) -> None:
            raise RuntimeError("boom")

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[StreamEvent] = []

        async def emit(self, event: StreamEvent) -> None:
            self.events.append(event)

    rec1, rec2 = _Recorder(), _Recorder()
    collector = TraceCollector("s8", "m1")
    fan = FanOutSink((rec1, _Boom(), collector, rec2))
    ev = make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1"))
    await fan.emit(ev)

    assert rec1.events == [ev] and rec2.events == [ev]  # not broken by Boom
    assert collector.model.turns == [] and collector.model.collector_errors == 0
    assert len(fan.errors) == 1  # the Boom failure was recorded
    assert fan.diagnostics[0]["error_class"] == "RuntimeError"
    assert fan.diagnostics[0]["event_type"] == "turn_start"


async def test_collector_records_fold_diagnostics() -> None:
    """Given a collector fold bug
    When emitting an event
    Then the observer failure is counted and summarized without raising."""
    collector = TraceCollector("s8b", "m1")

    def _boom(_event: StreamEvent) -> None:
        raise ValueError("bad fold")

    collector._fold = _boom  # type: ignore[method-assign]
    await collector.emit(make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")))

    assert collector.model.collector_errors == 1
    assert collector.model.collector_diagnostics[0]["error_class"] == "ValueError"
    assert collector.model.collector_diagnostics[0]["event_type"] == "turn_start"


# ── O9 — request snapshot per turn ────────────────────────────────────────────
async def test_o9_request_snapshot(tmp_path: Path) -> None:
    """Given a session that switches active skills mid-way
    When REQUEST events are folded
    Then each TurnRecord.request records system_sha256/tool_names/params."""
    collector = TraceCollector("s9", "m1")
    sys_text = "you are helpful"
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            _request_event(0, sys_text),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
            make_event(TurnStartPayload(turn_index=1, iteration=0, model="m1")),
            _request_event(1, sys_text),
            make_event(TurnEndPayload(turn_index=1, iterations=1, stop_cause="end_turn")),
        ],
    )
    req0 = collector.model.turns[0].request
    req1 = collector.model.turns[1].request
    assert req0 is not None and req1 is not None
    assert req0.system_sha256 == _sha(sys_text)
    assert req0.tool_names == ("read",)
    assert req0.max_tokens == 1000 and req0.temperature == 0.5
    assert req0.active_skills == ("plan",)
    assert req1.active_skills == ("plan", "review")


# ── O10 — system dedup: N turns with same system → one pool entry ─────────────
async def test_o10_system_dedup(tmp_path: Path) -> None:
    """Given N consecutive turns with an unchanged system prompt
    When folded
    Then system_prompts holds exactly one entry shared by sha256."""
    collector = TraceCollector("s10", "m1")
    sys_text = "stable system prompt"
    events: list[StreamEvent] = []
    for i in range(4):
        events.append(make_event(TurnStartPayload(turn_index=i, iteration=0, model="m1")))
        events.append(_request_event(i, sys_text))
        events.append(make_event(TurnEndPayload(turn_index=i, iterations=1, stop_cause="end_turn")))
    await _drive(collector, events)

    assert list(collector.model.system_prompts.keys()) == [_sha(sys_text)]
    shas = {t.request.system_sha256 for t in collector.model.turns if t.request}
    assert shas == {_sha(sys_text)}  # all four share one sha


# ── O11 — request losslessly reconstructable ──────────────────────────────────
async def test_o11_lossless_reconstruct(tmp_path: Path) -> None:
    """Given a persisted trace
    When a turn's request is rebuilt
    Then system full text (redacted) + tool_names + params are recoverable."""
    store = TraceStore(tmp_path)
    collector = TraceCollector("s11", "m1", store)
    sys_text = "system prompt body"
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            _request_event(0, sys_text, tools=("read", "bash")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    await collector.aclose()

    reloaded = await store.load("s11")
    assert reloaded is not None
    req = reloaded.turns[0].request
    assert req is not None
    assert reloaded.system_prompts[req.system_sha256] == sys_text
    assert req.tool_names == ("read", "bash")
    assert req.max_tokens == 1000


# ── O12 — system redaction: append with secret has no plaintext in pool ───────
async def test_o12_system_redaction(tmp_path: Path) -> None:
    """Given system_prompt_append containing a secret
    When folded
    Then the dedup pool's system text contains no plaintext secret."""
    secret = "sk-system-secret-9999"
    collector = TraceCollector("s12", "m1", redactor=Redactor([secret]))
    sys_text = f"base prompt\nappend: token={secret}"
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            _request_event(0, sys_text),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    stored = list(collector.model.system_prompts.values())[0]
    assert secret not in stored
    assert REDACTED in stored


# ── O13 — structured retry record ─────────────────────────────────────────────
async def test_o13_structured_retry(tmp_path: Path) -> None:
    """Given a 429 retry with transport switch
    When folding RETRY
    Then TurnRecord.retries is a structured RetryRecord with attempt/class/status."""
    collector = TraceCollector("s13", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(
                RetryPayload(
                    attempt=2,
                    reason="rate limited",
                    error_class="retryable",
                    http_status=429,
                    next_transport="anthropic.fallback.bearer",
                )
            ),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    retries = collector.model.turns[0].retries
    assert len(retries) == 1
    r = retries[0]
    assert r.attempt == 2 and r.http_status == 429
    assert r.error_class == "retryable"
    assert r.next_transport == "anthropic.fallback.bearer"


# ── O14 — structured error record (and session_errors when no active turn) ────
async def test_o14_structured_error(tmp_path: Path) -> None:
    """Given an unrecoverable backend error
    When folding ERROR
    Then TurnRecord.errors is a structured ErrorRecord; session_errors when no turn."""
    collector = TraceCollector("s14", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(
                ErrorPayload(
                    message="server exploded",
                    error_class="permanent",
                    http_status=500,
                    error_kind="bad_request",
                    phase="llm",
                )
            ),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="error")),
        ],
    )
    err = collector.model.turns[0].errors[0]
    assert err.http_status == 500
    assert err.phase is Phase.LLM
    assert err.error_kind is ErrorKind.BAD_REQUEST

    # Error before any turn → session_errors.
    c2 = TraceCollector("s14b", "m1")
    await c2.emit(make_event(ErrorPayload(message="startup failure", phase="transport")))
    assert len(c2.model.session_errors) == 1
    assert c2.model.session_errors[0].phase is Phase.TRANSPORT


# ── O15 — denied tool is visible in the trace ─────────────────────────────────
async def test_o15_denied_tool_visible(tmp_path: Path) -> None:
    """Given DenyAll + a delete call
    When folding TOOL_START(deny)/TOOL_END
    Then ToolCallRecord.decision=="deny", deny_reason set, kind=permission_denied."""
    collector = TraceCollector("s15", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(
                ApprovalPayload(
                    call_id="t1",
                    tool_name="delete",
                    summary="rm file",
                    reason="irreversible delete denied",
                )
            ),
            make_event(
                ToolStartPayload(
                    tool_use_id="t1",
                    tool_name="delete",
                    tool_input={"path": "x"},
                    decision="deny",
                )
            ),
            make_event(
                ToolEndPayload(
                    tool_use_id="t1",
                    tool_name="delete",
                    output="",
                    is_error=True,
                    error_kind="permission_denied",
                    error_message="denied by policy",
                )
            ),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    tool = collector.model.turns[0].tools[0]
    assert tool.decision == "deny" and tool.decision is ToolDecision.DENY
    assert tool.deny_reason == "irreversible delete denied"
    assert tool.error_kind == "permission_denied"
    assert tool.is_error is True


# ── O16 — structured tool failure reason ──────────────────────────────────────
async def test_o16_structured_tool_failure(tmp_path: Path) -> None:
    """Given a tool that times out / raises
    When folding TOOL_END
    Then is_error and error_kind ∈ {timeout, tool_error, aborted} with a message."""
    collector = TraceCollector("s16", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(ToolStartPayload(tool_use_id="t1", tool_name="bash", tool_input={})),
            make_event(
                ToolEndPayload(
                    tool_use_id="t1",
                    tool_name="bash",
                    output="",
                    is_error=True,
                    error_kind="timeout",
                    error_message="timed out after 30s; consider raising the timeout",
                )
            ),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    tool = collector.model.turns[0].tools[0]
    assert tool.is_error is True
    assert tool.error_kind in (ErrorKind.TIMEOUT, ErrorKind.TOOL_ERROR, ErrorKind.ABORTED)
    assert "timed out" in tool.error_message


# ── round-trip extra: TraceModel to_dict/from_dict ────────────────────────────
async def test_trace_model_round_trip(tmp_path: Path) -> None:
    collector = TraceCollector("rt", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(UsagePayload(usage=Usage(input_tokens=10, output_tokens=5), model="m1")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    d = collector.model.to_dict()
    rebuilt = TraceModel.from_dict(d)
    assert rebuilt.session_id == "rt"
    assert rebuilt.turns[0].llm is not None
    assert rebuilt.turns[0].llm.usage.input_tokens == 10


# ── regression: TurnRecord message_id anchors are filled at fold time ─────────
async def test_turn_message_id_anchors_filled(tmp_path: Path) -> None:
    """Given a turn that folds REQUEST + tool events
    When folded
    Then input/assistant/tool_result message_id anchors are non-empty and
    deterministically locate the turn (docs/reference/events-and-storage.md §4.3/§4.4bis)."""
    collector = TraceCollector("anchorsid", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            _request_event(0, "system body"),
            make_event(TextDeltaPayload(text="hello")),
            make_event(ToolStartPayload(tool_use_id="t1", tool_name="bash", tool_input={"command": "ls"})),
            make_event(ToolEndPayload(tool_use_id="t1", tool_name="bash", output="a.txt")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    turn = collector.model.turns[0]
    assert turn.input_message_id != ""
    assert turn.assistant_message_id != ""
    assert turn.tool_result_message_id != ""
    # anchors carry session + turn so the message is locatable
    assert "anchorsid" in turn.input_message_id
    assert str(turn.turn_index) in turn.input_message_id
    # the three anchors are distinct roles
    assert len({turn.input_message_id, turn.assistant_message_id, turn.tool_result_message_id}) == 3


async def test_turn_no_tool_has_no_tool_result_anchor(tmp_path: Path) -> None:
    """A turn without tool calls leaves tool_result_message_id empty."""
    collector = TraceCollector("notools", "m1")
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            _request_event(0, "system body"),
            make_event(TextDeltaPayload(text="hi")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    turn = collector.model.turns[0]
    assert turn.input_message_id != ""
    assert turn.assistant_message_id != ""
    assert turn.tool_result_message_id == ""


# ── regression: later-seen system prompt survives crash rebuild ───────────────
async def test_later_system_prompt_persisted_for_rebuild(tmp_path: Path) -> None:
    """Given turn 1 and turn 5 use different system prompts
    When the process crashes (no finalize) and we rebuild from jsonl
    Then BOTH system prompts are recoverable (docs/reference/events-and-storage.md §4.9)."""
    store = TraceStore(tmp_path)
    collector = TraceCollector("crashy", "m1", store)
    sys_a = "system ALPHA"
    sys_e = "system EPSILON"
    for i in range(6):
        sys = sys_a if i < 5 else sys_e
        await _drive(
            collector,
            [
                make_event(TurnStartPayload(turn_index=i, iteration=0, model="m1")),
                _request_event(i, sys),
                make_event(TurnEndPayload(turn_index=i, iterations=1, stop_cause="end_turn")),
            ],
        )
    # simulate crash: never call finalize(); rebuild only from the jsonl
    rebuilt = await store.rebuild_from_jsonl("crashy")
    assert rebuilt is not None
    assert rebuilt.system_prompts[_sha(sys_a)] == sys_a
    assert rebuilt.system_prompts[_sha(sys_e)] == sys_e
    # every turn's snapshot resolves to a present pool entry
    for turn in rebuilt.turns:
        assert turn.request is not None
        assert turn.request.system_sha256 in rebuilt.system_prompts


# ── regression: HTML export embeds JSON that JSON.parse can read back ──────────
async def test_html_export_embedded_json_is_parseable(tmp_path: Path) -> None:
    """Given a trace whose tool args contain quotes and angle brackets
    When exported to HTML
    Then the <script type=application/json> body round-trips via json.loads
    (no HTML-entity-escaped JSON; only </script> is neutralised)."""
    import re

    collector = TraceCollector("s_html", "m1")
    nasty = 'he said "hi" & <b>bold</b> </script><script>alert(1)'
    await _drive(
        collector,
        [
            make_event(TurnStartPayload(turn_index=0, iteration=0, model="m1")),
            make_event(ToolStartPayload(tool_use_id="t1", tool_name="bash", tool_input={"q": nasty})),
            make_event(ToolEndPayload(tool_use_id="t1", tool_name="bash", output="ok")),
            make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn")),
        ],
    )
    out = export_html(collector.model)
    m = re.search(r'<script id="trace-data" type="application/json">(.*?)</script>', out, re.S)
    assert m is not None
    body = m.group(1)
    # textContent decodes nothing, so a raw &quot; would break JSON.parse.
    assert "&quot;" not in body
    parsed = json.loads(body)
    assert parsed["trace"]["turns"][0]["tools"][0]["args"]["q"] == nasty
    # no literal </script> survives inside the data island
    assert "</script>" not in body
