"""Ports — Protocols that core depends on (docs/architecture/contracts.md §6).

core only depends on these; concrete adapters live in infra/frontends.
All ports are runtime_checkable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

from autoverse.types import (
    AbortReason,
    AbortSignal,
    Decision,
    ImageBlock,
    LLMRequest,
    Message,
    ProviderCapabilities,
    StreamEvent,
    ToolCall,
)

from autoverse.ports.diagnostics import Diagnostic, DiagnosticsPort
from autoverse.ports.context_archive import CompactionArchivePort
from autoverse.ports.turn_index import TurnIndexPort
from autoverse.ports.query import (
    STATUS_PRECEDENCE,
    SessionListQuery,
    SessionStatus,
    SessionSummary,
    merge_status,
)
from autoverse.ports.session_catalog import SessionCatalogPort, SessionProjection
from autoverse.ports.session_history import SessionHistoryPort, SessionHistoryWindow
from autoverse.ports.isolation import (
    FinalizeAction,
    IsoHandle,
    IsoResult,
    IsoSpec,
    IsolationError,
    Isolator,
    WorkspaceMode,
    WorkspaceSpec,
    WorktreeRegistry,
)
from autoverse.ports.snapshot import SnapshotPort, SnapshotRef
from autoverse.ports.browser import BrowserError, BrowserPort


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def stream(
        self, req: LLMRequest, signal: AbortSignal
    ) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(self, req: LLMRequest) -> int: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PermissionPort(Protocol):
    async def check(self, call: ToolCall) -> Decision: ...

    def check_sync(self, call: ToolCall) -> Decision: ...

    async def request_approval(self, call: ToolCall) -> Decision: ...

    def cancel_all(self) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: StreamEvent) -> None: ...


@dataclass
class SessionMeta:
    """Session-level metadata. Line 0 of a JSONL session file."""

    session_id: str
    model: str = ""
    cwd: str = ""
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    extra: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Store(Protocol):
    async def append(self, session_id: str, *msgs: Message) -> None: ...

    async def load(self, session_id: str, *, since: int = 0) -> list[Message]: ...

    async def rewrite(
        self, session_id: str, msgs: list[Message], reason: str
    ) -> None: ...

    async def create(self, session_id: str, meta: SessionMeta) -> None: ...

    async def load_meta(self, session_id: str) -> SessionMeta: ...

    async def update_meta(self, session_id: str, **fields: object) -> None: ...

    async def list_sessions(self) -> list[str]: ...

    async def delete(self, session_id: str) -> None: ...


@runtime_checkable
class SecretProvider(Protocol):
    def get(self, key: str) -> str | None: ...

    def require(self, key: str) -> str: ...


@runtime_checkable
class EngineHandle(Protocol):
    def run(
        self,
        prompt: str,
        attachments: tuple[ImageBlock, ...] = (),
        *,
        llm_prompt: str | None = None,
        message_metadata: dict[str, object] | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    def abort(self, reason: AbortReason = "cancel") -> None: ...

    async def sync_history(self) -> None: ...

    async def clear_in_place(self) -> int: ...

    async def compact(self) -> dict[str, int]: ...

    async def aclose(self, reason: str = "normal") -> None: ...

    def switch_model(self, model: str) -> None: ...

    @property
    def session_id(self) -> str: ...


__all__ = [
    "LLMProvider",
    "PermissionPort",
    "EventSink",
    "SessionMeta",
    "Store",
    "SecretProvider",
    "EngineHandle",
    "BrowserError",
    "BrowserPort",
    # read-only session listing (docs/architecture/frontend-adapters.md §4.2)
    "SessionListQuery",
    "SessionSummary",
    "SessionStatus",
    "SessionCatalogPort",
    "SessionProjection",
    "SessionHistoryPort",
    "SessionHistoryWindow",
    "STATUS_PRECEDENCE",
    "merge_status",
    # convenience re-exports used by adapters
    "AbortSignal",
    # snapshot / diagnostics
    "SnapshotPort",
    "SnapshotRef",
    "Diagnostic",
    "DiagnosticsPort",
    "CompactionArchivePort",
    "TurnIndexPort",
    # isolation (docs/reference/subagents.md §3.1 authoritative)
    "IsolationError",
    "IsoSpec",
    "IsoHandle",
    "FinalizeAction",
    "IsoResult",
    "WorkspaceMode",
    "WorkspaceSpec",
    "Isolator",
    "WorktreeRegistry",
]
