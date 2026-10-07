"""TraceCollector — an ``EventSink`` that folds StreamEvents into a TraceModel.

It is a peer of the frontend renderer: ``core`` only knows ``EventSink.emit`` and
never holds a reference to this collector (P3/O1). The collector reconstructs the
trace *only* from ``StreamEvent`` payloads — it never reads engine private state.

``emit`` never raises into ``core``: fold errors increment ``collector_errors``
(docs/reference/events-and-storage.md §3.1). Secrets are redacted at fold time so plaintext never
lands in the model/on disk (O7/O12).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from autoverse.infra.observ.meter import Meter
from autoverse.infra.observ.model import (
    CompactionKind,
    CompactionSegment,
    ErrorKind,
    ErrorRecord,
    GateDecisionRecord,
    LLMCallRecord,
    ParentLink,
    Phase,
    RequestSnapshot,
    RetryRecord,
    ToolCallRecord,
    ToolDecision,
    TraceModel,
    TurnRecord,
)
from autoverse.infra.observ.redact import Redactor
from autoverse.infra.observ.store import TraceStore
from autoverse.infra.observ.logging import log_context
from autoverse.infra.persist.serialize import msg_to_dict
from autoverse.types.events import (
    ApprovalPayload,
    CompactingPayload,
    ErrorPayload,
    EventType,
    GatePayload,
    RequestPayload,
    RetryPayload,
    StreamEvent,
    SubagentActivityPayload,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
)

#: Bounded preview ceiling for tool output / LLM text (docs/reference/events-and-storage.md §4.9).
PREVIEW_MAX = 8 * 1024
_LOG = logging.getLogger(__name__)


class TraceCollector:
    """Fold the StreamEvent flow into a structured ``TraceModel`` (O1)."""

    def __init__(
        self,
        session_id: str,
        model: str,
        store: TraceStore | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        parent: ParentLink | None = None,
        redactor: Redactor | None = None,
        meter: Meter | None = None,
        initial_model: TraceModel | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._wall = wall
        self._redactor = redactor or Redactor()
        self._meter = meter or Meter()
        self._model = initial_model or TraceModel.new(
            session_id, model, parent=parent, wall=wall()
        )
        if initial_model is not None:
            for turn in initial_model.turns:
                llm = turn.llm
                if llm is not None:
                    self._meter.record(llm.model or turn.model, llm.usage)
        self._model.end_reason = ""
        self._model.ended_wall = None
        self._cur: TurnRecord | None = None
        self._active_tools: dict[str, ToolCallRecord] = {}
        self._pending_approvals: dict[str, ApprovalPayload] = {}
        self._seg_index = max(
            (segment.index for segment in self._model.compaction_segments), default=0
        )

    @property
    def model(self) -> TraceModel:
        return self._model

    @property
    def meter(self) -> Meter:
        return self._meter

    async def emit(self, event: StreamEvent) -> None:
        """Sole entry point. Folds the event; never raises into core."""
        try:
            self._fold(event)
        except Exception as exc:  # noqa: BLE001 — observer must never poison the main path
            self._model.collector_errors += 1
            with log_context(session_id=self._model.session_id):
                _LOG.exception("trace collector failed to fold %s", event.type.value)
            self._model.collector_diagnostics.append(
                {
                    "event_type": event.type.value,
                    "error_class": type(exc).__name__,
                    "message": _bounded(str(exc) or type(exc).__name__),
                    "ts_mono": self._clock(),
                }
            )

    async def aclose(self, reason: str = "normal") -> None:
        """Lifecycle hook: finish in-flight tools, finalise, persist."""
        for rec in self._active_tools.values():
            if rec.end_mono is None:
                rec.end_mono = self._clock()
                rec.is_error = True
                rec.error_kind = ErrorKind.ABORTED
        if self._cur is not None:
            self._model.turns.append(self._cur)
            self._cur = None
        self._model.end_reason = reason
        self._model.ended_wall = self._wall()
        if self._store is not None:
            await self._store.finalize(self._model)

    # ── fold ─────────────────────────────────────────────────────────────────

    def _fold(self, event: StreamEvent) -> None:
        t = event.type
        p = event.payload
        if t is EventType.TURN_START and isinstance(p, TurnStartPayload):
            self._cur = TurnRecord(
                turn_index=p.turn_index, model=p.model, start_mono=self._clock()
            )
        elif t is EventType.REQUEST and isinstance(p, RequestPayload):
            self._fold_request(p)
        elif t is EventType.TEXT_DELTA and isinstance(p, TextDeltaPayload):
            self._llm().text_preview = _bounded(self._llm().text_preview + p.text)
        elif t is EventType.THINKING_DELTA and isinstance(p, ThinkingDeltaPayload):
            llm = self._llm()
            llm.thinking_preview = _bounded(llm.thinking_preview + p.text)
        elif t is EventType.USAGE and isinstance(p, UsagePayload):
            llm = self._llm(model=p.model)
            llm.usage = llm.usage + p.usage
            self._meter.record(p.model, p.usage)
        elif t is EventType.APPROVAL and isinstance(p, ApprovalPayload):
            self._pending_approvals[p.call_id] = p
        elif t is EventType.TOOL_START and isinstance(p, ToolStartPayload):
            self._fold_tool_start(p)
        elif t is EventType.TOOL_END and isinstance(p, ToolEndPayload):
            self._fold_tool_end(p)
        elif t is EventType.GATE_DECISION and isinstance(p, GatePayload):
            if self._cur is not None:
                self._cur.gate = GateDecisionRecord(
                    gate_name=p.gate_name, decision=p.decision, reason=p.reason
                )
        elif t is EventType.COMPACTING and isinstance(p, CompactingPayload):
            self._fold_compaction(p)
        elif t is EventType.RETRY and isinstance(p, RetryPayload):
            self._fold_retry(p)
        elif t is EventType.ERROR and isinstance(p, ErrorPayload):
            self._fold_error(p)
        elif t is EventType.SUBAGENT_ACTIVITY and isinstance(p, SubagentActivityPayload):
            self._fold_subagent(p)
        elif t is EventType.TURN_END and isinstance(p, TurnEndPayload):
            self._fold_turn_end(p)
        else:
            self._record_unknown(event)

    def _anchor(self, role: str) -> str:
        """Deterministic SessionStore-alignment anchor for the current turn.

        Events carry no message_id on the wire, so the collector derives a
        stable ``session_id:t<turn>:<role>`` anchor that locates the message by
        (session, turn, role) — the alignment key SessionStore replays against
        (docs/reference/events-and-storage.md §4.3/§4.4bis). Never empty for a real turn.
        """
        turn_index = self._cur.turn_index if self._cur is not None else 0
        return f"{self._model.session_id}:t{turn_index}:{role}"

    def _fold_request(self, p: RequestPayload) -> None:
        if p.system_sha256 not in self._model.system_prompts:
            self._model.system_prompts[p.system_sha256] = self._redactor.text(p.system)
        self._model.tool_schemas.setdefault(p.tools_sha256, list(p.tool_names))
        if self._cur is not None and not self._cur.input_message_id:
            self._cur.input_message_id = self._anchor("user")
        snap = RequestSnapshot(
            turn_index=p.turn_index,
            system_sha256=p.system_sha256,
            tools_sha256=p.tools_sha256,
            tool_names=p.tool_names,
            model=p.model,
            max_tokens=p.max_tokens,
            temperature=p.temperature,
            thinking_budget=p.thinking_budget,
            memory_sources=p.memory_sources,
            active_skills=p.active_skills,
            active_extensions=p.active_extensions,
        )
        if self._cur is not None:
            self._cur.request = snap

    def _fold_tool_start(self, p: ToolStartPayload) -> None:
        rec = ToolCallRecord(
            tool_use_id=p.tool_use_id,
            name=p.tool_name,
            args=self._redactor.mapping(p.tool_input),
            start_mono=self._clock(),
            decision=ToolDecision.parse(p.decision),
        )
        approval = self._pending_approvals.pop(p.tool_use_id, None)
        if approval is not None:
            rec.deny_reason = self._redactor.text(approval.reason)
        self._active_tools[p.tool_use_id] = rec
        if self._cur is not None:
            self._cur.tools.append(rec)
            if not self._cur.tool_result_message_id:
                self._cur.tool_result_message_id = self._anchor("tool_result")

    def _fold_tool_end(self, p: ToolEndPayload) -> None:
        rec = self._active_tools.pop(p.tool_use_id, None)
        if rec is None:
            return
        rec.end_mono = self._clock()
        rec.is_error = p.is_error
        rec.error_kind = ErrorKind.parse(p.error_kind)
        rec.error_message = self._redactor.text(p.error_message)
        rec.output_preview = _bounded(self._redactor.text(p.output))
        rec.output_truncated = len(p.output) > PREVIEW_MAX
        if rec.decision is ToolDecision.DENY and not rec.deny_reason and p.error_message:
            rec.deny_reason = self._redactor.text(p.error_message)

    def _fold_compaction(self, p: CompactingPayload) -> None:
        self._seg_index += 1
        kind = CompactionKind.parse(p.kind)
        seg_kind = CompactionKind.CLEAR if kind is CompactionKind.CLEAR else kind
        turn_idx = self._cur.turn_index if self._cur is not None else len(self._model.turns)
        messages = [msg_to_dict(self._redactor.message(m)) for m in p.folded]
        seg = CompactionSegment(
            kind=seg_kind,
            index=self._seg_index,
            turn_start=turn_idx,
            turn_end=turn_idx,
            summary_text=self._redactor.text(p.summary),
            archive_segment_id=p.archive_segment_id,
            folded_count=p.folded_count or len(p.folded),
            folded_sha256=p.folded_sha256,
            messages=messages,
        )
        self._model.compaction_segments.append(seg)
        if self._store is not None:
            self._store.append_compaction(self._model, seg)

    def _fold_retry(self, p: RetryPayload) -> None:
        rec = RetryRecord(
            attempt=p.attempt,
            reason=p.reason,
            error_class=p.error_class,
            http_status=p.http_status,
            next_model=p.next_model,
            next_transport=p.next_transport,
            ts_mono=self._clock(),
        )
        if self._cur is not None:
            self._cur.retries.append(rec)

    def _fold_error(self, p: ErrorPayload) -> None:
        rec = ErrorRecord(
            message=self._redactor.text(p.message),
            error_class=p.error_class,
            recoverable=p.recoverable,
            http_status=p.http_status,
            error_kind=ErrorKind.parse(p.error_kind),
            phase=Phase.parse(p.phase),
            ts_mono=self._clock(),
        )
        if self._cur is not None:
            self._cur.errors.append(rec)
        else:
            self._model.session_errors.append(rec)

    def _fold_subagent(self, p: SubagentActivityPayload) -> None:
        self._model.subagent_activity.append(
            {
                "job_id": p.job_id,
                "subagent_type": p.subagent_type,
                "activity": p.activity,
                "detail": self._redactor.text(p.detail),
                "child_session_id": p.child_session_id,
            }
        )

    def _fold_turn_end(self, p: TurnEndPayload) -> None:
        if self._cur is None:
            return
        self._cur.end_mono = self._clock()
        if self._cur.llm is not None:
            self._cur.llm.stop_cause = p.stop_cause
            if self._cur.llm.end_mono is None:
                self._cur.llm.end_mono = self._clock()
        finished = self._cur
        self._model.turns.append(finished)
        self._cur = None
        if self._store is not None:
            self._store.append_turn(self._model, finished)

    def _record_unknown(self, event: StreamEvent) -> None:
        entry: dict[str, object] = {
            "type": event.type.value,
            "payload": repr(event.payload),
        }
        if self._cur is not None:
            self._cur.unknown_events.append(entry)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _llm(self, *, model: str = "") -> LLMCallRecord:
        if self._cur is None:
            self._cur = TurnRecord(turn_index=0, model=model, start_mono=self._clock())
        if self._cur.llm is None:
            self._cur.llm = LLMCallRecord(
                model=model or self._cur.model, start_mono=self._clock()
            )
            if not self._cur.assistant_message_id:
                self._cur.assistant_message_id = self._anchor("assistant")
        elif model:
            self._cur.llm.model = model
        return self._cur.llm


def _bounded(text: str) -> str:
    return text if len(text) <= PREVIEW_MAX else text[:PREVIEW_MAX]


__all__ = ["TraceCollector", "PREVIEW_MAX"]
