"""Persistent memory tools.

These tools intentionally use a narrow, explicit file location under the current
workspace's ``.autoverse/agent-memory/<agent_type>/MEMORY.md`` by default. This
keeps them deterministic in tests and avoids silently mutating global user state.
Embedders may override the root via ``app.extra["memory_root"]``.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class MemoryReadArgs(BaseModel):
    agent_type: str = Field("main", description="Agent memory namespace to read.")


class MemoryWriteArgs(BaseModel):
    agent_type: str = Field("main", description="Agent memory namespace to write.")
    content: str = Field(..., description="Complete MEMORY.md contents.")
    append: bool = Field(False, description="Append instead of replacing.")


def _memory_path(ctx: ToolContext, agent_type: str) -> Path:
    root = ctx.app.extra.get("memory_root")
    base = Path(root) if isinstance(root, str | Path) else Path(ctx.cwd) / ".autoverse"
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in agent_type)
    return base / "agent-memory" / safe / "MEMORY.md"


@register_tool
class MemoryReadTool(BaseTool):
    name: ClassVar[str] = "memory_read"
    description: ClassVar[str] = (
        "Read persistent agent memory for an agent type from MEMORY.md."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = MemoryReadArgs

    async def run(self, args: MemoryReadArgs, ctx: ToolContext) -> ToolResult:
        path = _memory_path(ctx, args.agent_type)
        if not path.exists():
            return ToolResult.ok(
                f"No memory found for {args.agent_type!r}.",
                agent_type=args.agent_type,
                exists=False,
            )
        return ToolResult.ok(
            path.read_text(encoding="utf-8"),
            agent_type=args.agent_type,
            path=str(path),
            exists=True,
        )


@register_tool
class MemoryWriteTool(BaseTool):
    name: ClassVar[str] = "memory_write"
    description: ClassVar[str] = (
        "Write persistent agent memory for an agent type to MEMORY.md."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = MemoryWriteArgs

    async def run(self, args: MemoryWriteArgs, ctx: ToolContext) -> ToolResult:
        path = _memory_path(ctx, args.agent_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        if args.append and path.exists():
            previous = path.read_text(encoding="utf-8")
            text = previous.rstrip() + "\n\n" + args.content.strip() + "\n"
        else:
            text = args.content
        path.write_text(text, encoding="utf-8")
        return ToolResult.ok(
            f"Wrote memory for {args.agent_type!r} to {path}.",
            agent_type=args.agent_type,
            path=str(path),
            bytes=len(text.encode("utf-8")),
            append=args.append,
        )


__all__ = [
    "MemoryReadArgs",
    "MemoryReadTool",
    "MemoryWriteArgs",
    "MemoryWriteTool",
]
