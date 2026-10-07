"""Manager/session tools.

These are thin, capability-safe shells. Embedders that provide a real manager
backend can install it at ``app.extra["session_ops"]`` with async methods named
``spawn``, ``tell``, ``inbox``, ``abort`` and ``task_update``. ``SessionList`` can
also read the normal Store port directly, so it is useful in the default engine.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class SessionSpawnArgs(BaseModel):
    prompt: str = Field(..., description="Initial prompt for the new worker session.")
    model: str | None = Field(None, description="Optional model override.")


class SessionTellArgs(BaseModel):
    session_id: str
    message: str


class SessionInboxArgs(BaseModel):
    session_id: str | None = Field(None, description="Optional session filter.")


class SessionAbortArgs(BaseModel):
    session_id: str
    reason: str = "cancel"


class SessionTaskUpdateArgs(BaseModel):
    session_id: str
    status: str
    summary: str = ""


class NoArgs(BaseModel):
    pass


async def _delegate(ctx: ToolContext, method: str, payload: dict[str, Any]) -> ToolResult:
    ops = ctx.app.extra.get("session_ops")
    fn = getattr(ops, method, None)
    if not callable(fn):
        return ToolResult.error(
            f"No session manager backend configured for {method}.",
            error_kind="session_ops_unavailable",
        )
    out = await fn(**payload)
    # Echo the call's identifying fields into metadata so the TUI renderer can
    # show "{verb} [session_id]" without reflecting engine privates (P3).
    meta = {k: v for k, v in payload.items() if k in ("session_id", "status", "reason")}
    return ToolResult.ok(str(out), result=out, **meta)


@register_tool
class SessionSpawnTool(BaseTool):
    name: ClassVar[str] = "SessionSpawn"
    description: ClassVar[str] = "Spawn a managed worker session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = SessionSpawnArgs

    async def run(self, args: SessionSpawnArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "spawn", args.model_dump())


@register_tool
class SessionTellTool(BaseTool):
    name: ClassVar[str] = "SessionTell"
    description: ClassVar[str] = "Send a message to a managed worker session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = SessionTellArgs

    async def run(self, args: SessionTellArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "tell", args.model_dump())


@register_tool
class SendMessageTool(BaseTool):
    name: ClassVar[str] = "SendMessage"
    description: ClassVar[str] = "Compatibility alias for SessionTell."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = SessionTellArgs

    async def run(self, args: SessionTellArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "tell", args.model_dump())


@register_tool
class SessionInboxTool(BaseTool):
    name: ClassVar[str] = "SessionInbox"
    description: ClassVar[str] = "Read messages from managed worker sessions."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = SessionInboxArgs

    async def run(self, args: SessionInboxArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "inbox", args.model_dump())


@register_tool
class SessionListTool(BaseTool):
    name: ClassVar[str] = "SessionList"
    description: ClassVar[str] = "List known session ids from the active Store."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        lister = getattr(ctx.app.store, "list_sessions", None)
        if not callable(lister):
            return ToolResult.error("Store cannot list sessions.", error_kind="unsupported_store")
        sessions = await lister()
        return ToolResult.ok("\n".join(sessions) if sessions else "(no sessions)", count=len(sessions))


@register_tool
class SessionAbortTool(BaseTool):
    name: ClassVar[str] = "SessionAbort"
    description: ClassVar[str] = "Abort a managed worker session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = SessionAbortArgs

    async def run(self, args: SessionAbortArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "abort", args.model_dump())


@register_tool
class SessionTaskUpdateTool(BaseTool):
    name: ClassVar[str] = "SessionTaskUpdate"
    description: ClassVar[str] = "Update manager-visible task status for a session."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = SessionTaskUpdateArgs

    async def run(self, args: SessionTaskUpdateArgs, ctx: ToolContext) -> ToolResult:
        return await _delegate(ctx, "task_update", args.model_dump())


__all__ = [
    "SessionSpawnTool",
    "SessionTellTool",
    "SendMessageTool",
    "SessionInboxTool",
    "SessionListTool",
    "SessionAbortTool",
    "SessionTaskUpdateTool",
]
