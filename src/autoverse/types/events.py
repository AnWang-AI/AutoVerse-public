"""StreamEvent — the sole feedback channel core → frontends/trace (docs/architecture/contracts.md §3).

No bare-string event types. Each EventType maps to exactly one payload type via
PAYLOAD_FOR; contract tests guard full coverage and matching. All payloads are
frozen so they can be broadcast to multiple sinks (FanOut) safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from autoverse.types.messages import Message, Usage


class EventType(Enum):
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    COMPACTING = "compacting"
    GATE_DECISION = "gate_decision"
    USAGE = "usage"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    SUBAGENT_ACTIVITY = "subagent_activity"
    SUBAGENT_CHILD = "subagent_child"
    ERROR = "error"
    RETRY = "retry"
    APPROVAL = "approval"
    REQUEST = "request"


@dataclass(frozen=True)
class TextDeltaPayload:
    text: str


@dataclass(frozen=True)
class ThinkingDeltaPayload:
    text: str


@dataclass(frozen=True)
class ToolStartPayload:
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, object]
    decision: str = "allow"  # allow | deny | ask


@dataclass(frozen=True)
class ToolEndPayload:
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
    error_kind: str = ""
    error_message: str = ""
    duration_ms: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CompactingPayload:
    kind: str  # micro | meta | truncate | clear | overflow | overflow_truncate | overflow_compact
    tokens_before: int = 0
    tokens_after: int = 0
    messages_before: int = 0
    messages_after: int = 0
    summary: str = ""
    archive_segment_id: str = ""
    folded_count: int = 0
    folded_sha256: str = ""
    # Legacy compatibility only. Production compaction persists removed messages
    # in CompactionArchive and emits the reference above instead of raw content.
    folded: tuple[Message, ...] = ()


@dataclass(frozen=True)
class GatePayload:
    gate_name: str
    decision: str  # stop | continue | need_user
    reason: str = ""


@dataclass(frozen=True)
class UsagePayload:
    usage: Usage
    model: str
    cost_usd: float = 0.0


@dataclass(frozen=True)
class TurnStartPayload:
    turn_index: int
    iteration: int
    model: str


@dataclass(frozen=True)
class TurnEndPayload:
    turn_index: int
    iterations: int
    stop_cause: str


@dataclass(frozen=True)
class SubagentActivityPayload:
    job_id: str
    subagent_type: str
    activity: str
    detail: str = ""
    child_session_id: str | None = None
    # Lifecycle state of the dispatch (docs/reference/subagents.md §13): "" (legacy/unknown) |
    # pending | running | done | failed | cancelled. Front-ends key styling on
    # this; defaulting to "" keeps every existing emit backward-compatible.
    status: str = ""
    turn: int = 0            # child turn index for "running" heartbeats (0 = n/a)
    elapsed_ms: float = 0.0  # wall-clock since dispatch, for live/completion display
    # Child tool calls observed so far — feeds the MiMo `N toolcalls` completion
    # footer. Set on the terminal "done" emit (0 = unknown / not yet counted).
    tool_count: int = 0
    # Short task brief shown persistently on the card head — MiMo's
    # ``│ {Type} Task — {description}``. Distinct from ``detail`` (the transient
    # activity/turn label). Front-ends may inject it from a per-job record so it
    # survives heartbeats that don't repeat it. "" → head shows just the title.
    description: str = ""
    # Full dispatch task (the sub-agent's "user prompt"). Set ONCE on the initial
    # "running" emit so a front-end can echo it at the top of the drill-down
    # transcript exactly like the main conversation echoes the user message.
    # ``detail`` keeps the short (≤80-char) sidebar label; this is the untruncated
    # instruction. Defaults to "" → backward-compatible with every other emit.
    task: str = ""


@dataclass(frozen=True)
class SubagentChildPayload:
    """One raw child-engine event, tagged with its owning sub-agent (drill-down).

    The progress sink suppresses a sub-agent's raw cards from the parent
    scrollback (they would leak the child's internals — see ``app/subagent``),
    but wraps each one here so a front-end can RETAIN it in a per-``job_id``
    transcript the user can open on demand ("enter the sub-agent's context").
    ``inner`` is the child's original :class:`StreamEvent`; consumers that don't
    care about drill-down ignore this event type entirely (collector fallback)."""

    job_id: str
    subagent_type: str
    inner: "StreamEvent"


@dataclass(frozen=True)
class ErrorPayload:
    message: str
    error_class: str = "unknown"
    recoverable: bool = False
    http_status: int | None = None
    error_kind: str = ""
    platform_code: str | int | None = None
    phase: str = ""  # llm | tool | gate | compaction | transport


@dataclass(frozen=True)
class RetryPayload:
    attempt: int
    reason: str
    error_class: str = "unknown"
    http_status: int | None = None
    platform_code: str | int | None = None
    delay_seconds: float | None = None
    next_model: str | None = None
    next_transport: str | None = None


@dataclass(frozen=True)
class ApprovalPayload:
    call_id: str
    tool_name: str
    summary: str
    reason: str = ""


@dataclass(frozen=True)
class RequestPayload:
    turn_index: int
    system: str
    system_sha256: str
    tool_names: tuple[str, ...]
    tools_sha256: str
    model: str
    max_tokens: int
    temperature: float
    thinking_budget: int
    memory_sources: tuple[str, ...] = ()
    active_skills: tuple[str, ...] = ()
    active_extensions: tuple[str, ...] = ()


Payload = (
    TextDeltaPayload
    | ThinkingDeltaPayload
    | ToolStartPayload
    | ToolEndPayload
    | CompactingPayload
    | GatePayload
    | UsagePayload
    | TurnStartPayload
    | TurnEndPayload
    | SubagentActivityPayload
    | SubagentChildPayload
    | ErrorPayload
    | RetryPayload
    | ApprovalPayload
    | RequestPayload
)


PAYLOAD_FOR: dict[EventType, type] = {
    EventType.TEXT_DELTA: TextDeltaPayload,
    EventType.THINKING_DELTA: ThinkingDeltaPayload,
    EventType.TOOL_START: ToolStartPayload,
    EventType.TOOL_END: ToolEndPayload,
    EventType.COMPACTING: CompactingPayload,
    EventType.GATE_DECISION: GatePayload,
    EventType.USAGE: UsagePayload,
    EventType.TURN_START: TurnStartPayload,
    EventType.TURN_END: TurnEndPayload,
    EventType.SUBAGENT_ACTIVITY: SubagentActivityPayload,
    EventType.SUBAGENT_CHILD: SubagentChildPayload,
    EventType.ERROR: ErrorPayload,
    EventType.RETRY: RetryPayload,
    EventType.APPROVAL: ApprovalPayload,
    EventType.REQUEST: RequestPayload,
}

# Reverse map: payload type -> event type (for make_event).
_TYPE_FOR_PAYLOAD: dict[type, EventType] = {v: k for k, v in PAYLOAD_FOR.items()}


@dataclass(frozen=True)
class StreamEvent:
    type: EventType
    payload: Payload

    def __post_init__(self) -> None:
        expected = PAYLOAD_FOR.get(self.type)
        if expected is None:
            raise ValueError(f"No payload registered for {self.type}")
        if type(self.payload) is not expected:
            raise TypeError(
                f"StreamEvent.type {self.type} expects {expected.__name__}, "
                f"got {type(self.payload).__name__}"
            )


def make_event(payload: Payload) -> StreamEvent:
    """Build a StreamEvent by reverse-looking-up its type. Guards against mismatch."""
    event_type = _TYPE_FOR_PAYLOAD.get(type(payload))
    if event_type is None:
        raise TypeError(f"Unknown payload type: {type(payload).__name__}")
    return StreamEvent(type=event_type, payload=payload)


__all__ = [
    "EventType",
    "TextDeltaPayload",
    "ThinkingDeltaPayload",
    "ToolStartPayload",
    "ToolEndPayload",
    "CompactingPayload",
    "GatePayload",
    "UsagePayload",
    "TurnStartPayload",
    "TurnEndPayload",
    "SubagentActivityPayload",
    "SubagentChildPayload",
    "ErrorPayload",
    "RetryPayload",
    "ApprovalPayload",
    "RequestPayload",
    "Payload",
    "PAYLOAD_FOR",
    "StreamEvent",
    "make_event",
]
