"""autoverse.types — value types (single source of truth, docs/architecture/contracts.md §1-5).

Re-exports all shared value types. There is exactly one definition per type;
this module only re-exports, it does not redefine (guards C9).
"""

from __future__ import annotations

from autoverse.types.abort import (
    AbortController,
    AbortError,
    AbortReason,
    AbortSignal,
)
from autoverse.types.capability import CapabilityTier
from autoverse.types.browser import BrowserPageObservation
from autoverse.types.context import (
    CompactionArchiveDiagnostics,
    CompactionCatalogEntry,
    CompactionSearchHit,
    CompactionSegmentRef,
    CompactionSlice,
)
from autoverse.types.events import (
    PAYLOAD_FOR,
    ApprovalPayload,
    CompactingPayload,
    ErrorPayload,
    EventType,
    GatePayload,
    Payload,
    RequestPayload,
    RetryPayload,
    StreamEvent,
    SubagentActivityPayload,
    SubagentChildPayload,
    TextDeltaPayload,
    ThinkingDeltaPayload,
    ToolEndPayload,
    ToolStartPayload,
    TurnEndPayload,
    TurnStartPayload,
    UsagePayload,
    make_event,
)
from autoverse.types.llm import (
    AuthKind,
    ErrorClass,
    LLMRequest,
    ProviderCapabilities,
    ToolSchema,
    WireKind,
)
from autoverse.types.messages import (
    ContentBlock,
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from autoverse.types.tools import Decision, ToolCall, ToolResult

__all__ = [
    # abort
    "AbortController",
    "AbortError",
    "AbortReason",
    "AbortSignal",
    # capability
    "CapabilityTier",
    # browser
    "BrowserPageObservation",
    # compacted context
    "CompactionSearchHit",
    "CompactionArchiveDiagnostics",
    "CompactionCatalogEntry",
    "CompactionSegmentRef",
    "CompactionSlice",
    # events
    "EventType",
    "StreamEvent",
    "make_event",
    "PAYLOAD_FOR",
    "Payload",
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
    # llm
    "AuthKind",
    "ErrorClass",
    "LLMRequest",
    "ProviderCapabilities",
    "ToolSchema",
    "WireKind",
    # messages
    "ContentBlock",
    "ImageBlock",
    "Message",
    "Role",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
    # tools / permission
    "Decision",
    "ToolCall",
    "ToolResult",
]
