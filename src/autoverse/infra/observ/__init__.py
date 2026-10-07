"""Observability subsystem (docs/reference/events-and-storage.md).

A :class:`~autoverse.ports.EventSink` implementation (``TraceCollector``) that
folds the ``StreamEvent`` flow into a structured ``TraceModel``, plus metering,
redaction, persistence (``TraceStore``), export and a read-only viewer data API.

This subsystem lives in ``infra`` and is never imported by ``core`` (P3).
"""

from __future__ import annotations

from autoverse.infra.observ.collector import TraceCollector
from autoverse.infra.observ.fanout import FanOutSink
from autoverse.infra.observ.meter import CostBreakdown, Meter, PRICING
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
    SCHEMA_VERSION,
    ToolCallRecord,
    ToolDecision,
    TraceModel,
    TurnRecord,
)
from autoverse.infra.observ.redact import Redactor
from autoverse.infra.observ.store import TraceStore

__all__ = [
    "TraceCollector",
    "FanOutSink",
    "Meter",
    "CostBreakdown",
    "PRICING",
    "Redactor",
    "TraceStore",
    "TraceModel",
    "TurnRecord",
    "RequestSnapshot",
    "RetryRecord",
    "ErrorRecord",
    "ToolCallRecord",
    "LLMCallRecord",
    "GateDecisionRecord",
    "CompactionSegment",
    "ParentLink",
    "Phase",
    "ToolDecision",
    "ErrorKind",
    "CompactionKind",
    "SCHEMA_VERSION",
]
