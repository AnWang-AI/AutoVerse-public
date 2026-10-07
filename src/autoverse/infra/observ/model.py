"""TraceModel — structured trace domain model (docs/reference/events-and-storage.md §3.3/§9.5/§9.6).

The event stream is folded into these frozen dataclasses. All categorical fields
use real string-valued enums (``Phase``/``ToolDecision``/``ErrorKind``/
``CompactionKind``) so "why did it fail / retry / get denied" is structured data,
never free-text parsing (KDR-9). Each record is derived from exactly one
``StreamEvent`` payload.

The enums subclass ``str`` so callers can both pattern-match on the enum *and*
compare against the wire string (e.g. ``rec.decision == "deny"``) — the
StreamEvent payloads carry these categories as bare strings on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from autoverse.types.messages import Message, Usage

SCHEMA_VERSION = 1


class Phase(str, Enum):
    """Phase at which an error occurred (ErrorPayload.phase)."""

    LLM = "llm"
    TOOL = "tool"
    GATE = "gate"
    COMPACTION = "compaction"
    TRANSPORT = "transport"
    UNKNOWN = ""

    @classmethod
    def parse(cls, value: str) -> "Phase":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class ToolDecision(str, Enum):
    """Permission verdict carried on ToolStartPayload.decision."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

    @classmethod
    def parse(cls, value: str) -> "ToolDecision":
        try:
            return cls(value)
        except ValueError:
            return cls.ALLOW


class ErrorKind(str, Enum):
    """Structured failure kind (ToolEndPayload.error_kind / ErrorPayload.error_kind)."""

    NONE = ""
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    ABORTED = "aborted"
    TOOL_ERROR = "tool_error"
    UNSUPPORTED_RESULT = "unsupported_result"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "ErrorKind":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class CompactionKind(str, Enum):
    """Kind of a compaction/clear segment (CompactingPayload.kind)."""

    MICRO = "micro"
    META = "meta"
    TRUNCATE = "truncate"
    OVERFLOW = "overflow"
    OVERFLOW_TRUNCATE = "overflow_truncate"
    OVERFLOW_COMPACT = "overflow_compact"
    CLEAR = "clear"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "CompactionKind":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@dataclass
class ErrorRecord:
    """Structured error record folded from ``ErrorPayload`` (O14)."""

    message: str
    error_class: str = "unknown"
    recoverable: bool = False
    http_status: int | None = None
    error_kind: ErrorKind = ErrorKind.NONE
    phase: Phase = Phase.UNKNOWN
    attempt: int = 0
    ts_mono: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "message": self.message,
            "error_class": self.error_class,
            "recoverable": self.recoverable,
            "http_status": self.http_status,
            "error_kind": self.error_kind.value,
            "phase": self.phase.value,
            "attempt": self.attempt,
            "ts_mono": self.ts_mono,
        }


@dataclass
class RetryRecord:
    """Structured retry record folded from ``RetryPayload`` (O13)."""

    attempt: int
    reason: str = ""
    error_class: str = "unknown"
    http_status: int | None = None
    next_model: str | None = None
    next_transport: str | None = None
    ts_mono: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "reason": self.reason,
            "error_class": self.error_class,
            "http_status": self.http_status,
            "next_model": self.next_model,
            "next_transport": self.next_transport,
            "ts_mono": self.ts_mono,
        }


@dataclass
class LLMCallRecord:
    model: str
    start_mono: float
    end_mono: float | None = None
    usage: Usage = field(default_factory=Usage)
    stop_cause: str = ""
    text_preview: str = ""
    text_truncated: bool = False
    thinking_preview: str = ""

    @property
    def duration_ms(self) -> float:
        return 0.0 if self.end_mono is None else (self.end_mono - self.start_mono) * 1e3

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "start_mono": self.start_mono,
            "end_mono": self.end_mono,
            "usage": _usage_to_dict(self.usage),
            "stop_cause": self.stop_cause,
            "text_preview": self.text_preview,
            "text_truncated": self.text_truncated,
            "thinking_preview": self.thinking_preview,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ToolCallRecord:
    tool_use_id: str
    name: str
    args: dict[str, object]
    start_mono: float = 0.0
    end_mono: float | None = None
    is_error: bool = False
    error_kind: ErrorKind = ErrorKind.NONE
    error_message: str = ""
    decision: ToolDecision = ToolDecision.ALLOW
    deny_reason: str = ""
    output_preview: str = ""
    output_truncated: bool = False

    @property
    def duration_ms(self) -> float:
        return 0.0 if self.end_mono is None else (self.end_mono - self.start_mono) * 1e3

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_use_id": self.tool_use_id,
            "name": self.name,
            "args": self.args,
            "start_mono": self.start_mono,
            "end_mono": self.end_mono,
            "is_error": self.is_error,
            "error_kind": self.error_kind.value,
            "error_message": self.error_message,
            "decision": self.decision.value,
            "deny_reason": self.deny_reason,
            "output_preview": self.output_preview,
            "output_truncated": self.output_truncated,
            "duration_ms": self.duration_ms,
        }


@dataclass
class GateDecisionRecord:
    gate_name: str
    decision: str
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"gate_name": self.gate_name, "decision": self.decision, "reason": self.reason}


@dataclass
class RequestSnapshot:
    """Non-message part of the assembled LLMRequest (O9/O11)."""

    turn_index: int
    system_sha256: str
    tools_sha256: str
    tool_names: tuple[str, ...] = ()
    model: str = ""
    max_tokens: int = 0
    temperature: float = 0.0
    thinking_budget: int = 0
    memory_sources: tuple[str, ...] = ()
    active_skills: tuple[str, ...] = ()
    active_extensions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_index": self.turn_index,
            "system_sha256": self.system_sha256,
            "tools_sha256": self.tools_sha256,
            "tool_names": list(self.tool_names),
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "thinking_budget": self.thinking_budget,
            "memory_sources": list(self.memory_sources),
            "active_skills": list(self.active_skills),
            "active_extensions": list(self.active_extensions),
        }


@dataclass
class TurnRecord:
    turn_index: int
    model: str
    start_mono: float
    end_mono: float | None = None
    llm: LLMCallRecord | None = None
    tools: list[ToolCallRecord] = field(default_factory=list)
    gate: GateDecisionRecord | None = None
    request: RequestSnapshot | None = None
    retries: list[RetryRecord] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    unknown_events: list[dict[str, object]] = field(default_factory=list)
    input_message_id: str = ""
    assistant_message_id: str = ""
    tool_result_message_id: str = ""

    @property
    def total_duration_ms(self) -> float:
        return 0.0 if self.end_mono is None else (self.end_mono - self.start_mono) * 1e3

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_index": self.turn_index,
            "model": self.model,
            "start_mono": self.start_mono,
            "end_mono": self.end_mono,
            "llm": None if self.llm is None else self.llm.to_dict(),
            "tools": [t.to_dict() for t in self.tools],
            "gate": None if self.gate is None else self.gate.to_dict(),
            "request": None if self.request is None else self.request.to_dict(),
            "retries": [r.to_dict() for r in self.retries],
            "errors": [e.to_dict() for e in self.errors],
            "unknown_events": list(self.unknown_events),
            "input_message_id": self.input_message_id,
            "assistant_message_id": self.assistant_message_id,
            "tool_result_message_id": self.tool_result_message_id,
            "total_duration_ms": self.total_duration_ms,
        }


@dataclass
class CompactionSegment:
    """append-only record of one compaction/clear (P5)."""

    kind: CompactionKind
    index: int
    turn_start: int
    turn_end: int
    summary_text: str = ""
    archive_segment_id: str = ""
    folded_count: int = 0
    folded_sha256: str = ""
    messages: list[dict[str, object]] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "index": self.index,
            "turn_start": self.turn_start,
            "turn_end": self.turn_end,
            "summary_text": self.summary_text,
            "archive_segment_id": self.archive_segment_id,
            "folded_count": self.folded_count,
            "folded_sha256": self.folded_sha256,
            "messages": list(self.messages),
            "truncated": self.truncated,
        }


@dataclass
class ParentLink:
    parent_session_id: str
    parent_tool_id: str
    subagent_type: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_tool_id": self.parent_tool_id,
            "subagent_type": self.subagent_type,
            "description": self.description,
        }


@dataclass
class TraceModel:
    schema_version: int
    session_id: str
    created_at: str
    model: str
    started_wall: float
    ended_wall: float | None = None
    end_reason: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    compaction_segments: list[CompactionSegment] = field(default_factory=list)
    session_errors: list[ErrorRecord] = field(default_factory=list)
    parent: ParentLink | None = None
    subagent_activity: list[dict[str, object]] = field(default_factory=list)
    system_prompts: dict[str, str] = field(default_factory=dict)
    tool_schemas: dict[str, list[str]] = field(default_factory=dict)
    collector_errors: int = 0
    collector_diagnostics: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        session_id: str,
        model: str,
        *,
        parent: ParentLink | None = None,
        wall: float = 0.0,
    ) -> "TraceModel":
        return cls(
            schema_version=SCHEMA_VERSION,
            session_id=session_id,
            created_at=datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()
            if wall
            else datetime.now(timezone.utc).isoformat(),
            model=model,
            started_wall=wall,
            parent=parent,
        )

    @property
    def usage(self) -> Usage:
        total = Usage()
        for turn in self.turns:
            if turn.llm is not None:
                total = total + turn.llm.usage
        return total

    @property
    def total_duration_ms(self) -> float:
        return sum(t.total_duration_ms for t in self.turns)

    def header_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "model": self.model,
            "started_wall": self.started_wall,
            "parent": None if self.parent is None else self.parent.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.header_dict(),
            "ended_wall": self.ended_wall,
            "end_reason": self.end_reason,
            "turns": [t.to_dict() for t in self.turns],
            "compaction_segments": [s.to_dict() for s in self.compaction_segments],
            "session_errors": [e.to_dict() for e in self.session_errors],
            "subagent_activity": list(self.subagent_activity),
            "system_prompts": dict(self.system_prompts),
            "tool_schemas": {k: list(v) for k, v in self.tool_schemas.items()},
            "collector_errors": self.collector_errors,
            "collector_diagnostics": list(self.collector_diagnostics),
            "usage": _usage_to_dict(self.usage),
            "total_duration_ms": self.total_duration_ms,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "TraceModel":
        return _trace_from_dict(d)


def _usage_to_dict(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
        "total": u.total,
    }


def _usage_from_dict(d: dict[str, object]) -> Usage:
    return Usage(
        input_tokens=_int(d.get("input_tokens")),
        output_tokens=_int(d.get("output_tokens")),
        cache_read_tokens=_int(d.get("cache_read_tokens")),
        cache_write_tokens=_int(d.get("cache_write_tokens")),
    )


def _int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _str_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return ()


def _dict_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _opt_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _opt_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _llm_from_dict(d: dict[str, object]) -> LLMCallRecord:
    usage = d.get("usage", {})
    return LLMCallRecord(
        model=str(d.get("model", "")),
        start_mono=_float(d.get("start_mono")),
        end_mono=_opt_float(d.get("end_mono")),
        usage=_usage_from_dict(usage) if isinstance(usage, dict) else Usage(),
        stop_cause=str(d.get("stop_cause", "")),
        text_preview=str(d.get("text_preview", "")),
        text_truncated=bool(d.get("text_truncated", False)),
        thinking_preview=str(d.get("thinking_preview", "")),
    )


def _tool_from_dict(d: dict[str, object]) -> ToolCallRecord:
    args = d.get("args", {})
    return ToolCallRecord(
        tool_use_id=str(d.get("tool_use_id", "")),
        name=str(d.get("name", "")),
        args=dict(args) if isinstance(args, dict) else {},
        start_mono=_float(d.get("start_mono")),
        end_mono=_opt_float(d.get("end_mono")),
        is_error=bool(d.get("is_error", False)),
        error_kind=ErrorKind.parse(str(d.get("error_kind", ""))),
        error_message=str(d.get("error_message", "")),
        decision=ToolDecision.parse(str(d.get("decision", "allow"))),
        deny_reason=str(d.get("deny_reason", "")),
        output_preview=str(d.get("output_preview", "")),
        output_truncated=bool(d.get("output_truncated", False)),
    )


def _retry_from_dict(d: dict[str, object]) -> RetryRecord:
    return RetryRecord(
        attempt=_int(d.get("attempt")),
        reason=str(d.get("reason", "")),
        error_class=str(d.get("error_class", "unknown")),
        http_status=_opt_int(d.get("http_status")),
        next_model=None if d.get("next_model") is None else str(d.get("next_model")),
        next_transport=None
        if d.get("next_transport") is None
        else str(d.get("next_transport")),
        ts_mono=_float(d.get("ts_mono")),
    )


def _error_from_dict(d: dict[str, object]) -> ErrorRecord:
    return ErrorRecord(
        message=str(d.get("message", "")),
        error_class=str(d.get("error_class", "unknown")),
        recoverable=bool(d.get("recoverable", False)),
        http_status=_opt_int(d.get("http_status")),
        error_kind=ErrorKind.parse(str(d.get("error_kind", ""))),
        phase=Phase.parse(str(d.get("phase", ""))),
        attempt=_int(d.get("attempt")),
        ts_mono=_float(d.get("ts_mono")),
    )


def _request_from_dict(d: dict[str, object]) -> RequestSnapshot:
    return RequestSnapshot(
        turn_index=_int(d.get("turn_index")),
        system_sha256=str(d.get("system_sha256", "")),
        tools_sha256=str(d.get("tools_sha256", "")),
        tool_names=_str_tuple(d.get("tool_names")),
        model=str(d.get("model", "")),
        max_tokens=_int(d.get("max_tokens")),
        temperature=_float(d.get("temperature")),
        thinking_budget=_int(d.get("thinking_budget")),
        memory_sources=_str_tuple(d.get("memory_sources")),
        active_skills=_str_tuple(d.get("active_skills")),
        active_extensions=_str_tuple(d.get("active_extensions")),
    )


def _gate_from_dict(d: dict[str, object]) -> GateDecisionRecord:
    return GateDecisionRecord(
        gate_name=str(d.get("gate_name", "")),
        decision=str(d.get("decision", "")),
        reason=str(d.get("reason", "")),
    )


def _turn_from_dict(d: dict[str, object]) -> TurnRecord:
    llm = d.get("llm")
    gate = d.get("gate")
    request = d.get("request")
    return TurnRecord(
        turn_index=_int(d.get("turn_index")),
        model=str(d.get("model", "")),
        start_mono=_float(d.get("start_mono")),
        end_mono=_opt_float(d.get("end_mono")),
        llm=_llm_from_dict(llm) if isinstance(llm, dict) else None,
        tools=[_tool_from_dict(t) for t in _dict_list(d.get("tools"))],
        gate=_gate_from_dict(gate) if isinstance(gate, dict) else None,
        request=_request_from_dict(request) if isinstance(request, dict) else None,
        retries=[_retry_from_dict(r) for r in _dict_list(d.get("retries"))],
        errors=[_error_from_dict(e) for e in _dict_list(d.get("errors"))],
        unknown_events=_dict_list(d.get("unknown_events")),
        input_message_id=str(d.get("input_message_id", "")),
        assistant_message_id=str(d.get("assistant_message_id", "")),
        tool_result_message_id=str(d.get("tool_result_message_id", "")),
    )


def _seg_from_dict(d: dict[str, object]) -> CompactionSegment:
    return CompactionSegment(
        kind=CompactionKind.parse(str(d.get("kind", "unknown"))),
        index=_int(d.get("index")),
        turn_start=_int(d.get("turn_start")),
        turn_end=_int(d.get("turn_end")),
        summary_text=str(d.get("summary_text", "")),
        archive_segment_id=str(d.get("archive_segment_id", "")),
        folded_count=_int(d.get("folded_count")),
        folded_sha256=str(d.get("folded_sha256", "")),
        messages=_dict_list(d.get("messages")),
        truncated=bool(d.get("truncated", False)),
    )


def _parent_from_dict(d: dict[str, object]) -> ParentLink:
    return ParentLink(
        parent_session_id=str(d.get("parent_session_id", "")),
        parent_tool_id=str(d.get("parent_tool_id", "")),
        subagent_type=str(d.get("subagent_type", "")),
        description=str(d.get("description", "")),
    )


def _trace_from_dict(d: dict[str, object]) -> TraceModel:
    parent = d.get("parent")
    schemas = d.get("tool_schemas", {})
    prompts = d.get("system_prompts", {})
    model = TraceModel(
        schema_version=_int(d.get("schema_version")),
        session_id=str(d.get("session_id", "")),
        created_at=str(d.get("created_at", "")),
        model=str(d.get("model", "")),
        started_wall=_float(d.get("started_wall")),
        ended_wall=_opt_float(d.get("ended_wall")),
        end_reason=str(d.get("end_reason", "")),
        parent=_parent_from_dict(parent) if isinstance(parent, dict) else None,
    )
    model.turns = [_turn_from_dict(t) for t in _dict_list(d.get("turns"))]
    model.compaction_segments = [
        _seg_from_dict(s) for s in _dict_list(d.get("compaction_segments"))
    ]
    model.session_errors = [
        _error_from_dict(e) for e in _dict_list(d.get("session_errors"))
    ]
    model.subagent_activity = _dict_list(d.get("subagent_activity"))
    if isinstance(prompts, dict):
        model.system_prompts = {str(k): str(v) for k, v in prompts.items()}
    if isinstance(schemas, dict):
        model.tool_schemas = {str(k): list(v) for k, v in schemas.items()}
    model.collector_errors = _int(d.get("collector_errors"))
    model.collector_diagnostics = _dict_list(d.get("collector_diagnostics"))
    return model


# Message is re-exported for type hints used by collector/export helpers.
_ = Message

__all__ = [
    "SCHEMA_VERSION",
    "Phase",
    "ToolDecision",
    "ErrorKind",
    "CompactionKind",
    "ErrorRecord",
    "RetryRecord",
    "LLMCallRecord",
    "ToolCallRecord",
    "GateDecisionRecord",
    "RequestSnapshot",
    "TurnRecord",
    "CompactionSegment",
    "ParentLink",
    "TraceModel",
]
