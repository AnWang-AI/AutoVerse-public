"""ThinkTool + TodoWriteTool (docs/reference/tools.md).

ThinkTool is a zero-side-effect scratchpad (READ_ONLY, never errors). TodoWriteTool
persists a session-isolated todo list; persistence failures are reported through
the event sink rather than raising.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class ThinkArgs(BaseModel):
    thought: str = Field(..., description="Reasoning text (private scratchpad).")


@register_tool
class ThinkTool(BaseTool):
    name: ClassVar[str] = "think"
    description: ClassVar[str] = "Think through a problem privately (no side effects)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = ThinkArgs

    async def run(self, args: ThinkArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(
            f"Recorded thought ({len(args.thought)} chars).", chars=len(args.thought)
        )


class TodoItem(BaseModel):
    id: str
    content: str
    status: str = "pending"
    priority: str = "medium"


class TodoWriteArgs(BaseModel):
    todos: list[TodoItem] = Field(default_factory=list)


@register_tool
class TodoWriteTool(BaseTool):
    name: ClassVar[str] = "todo_write"
    description: ClassVar[str] = "Create/replace the session todo list."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    Args: ClassVar[type[BaseModel]] = TodoWriteArgs

    @staticmethod
    def _todo_meta(todos: list["TodoItem"]) -> list[dict[str, str]]:
        """The todo list as plain dicts for ToolResult metadata (P3): the TUI
        sidebar renders this off the event, never reflecting private state."""
        return [{"status": t.status, "content": t.content} for t in todos]

    async def run(self, args: TodoWriteArgs, ctx: ToolContext) -> ToolResult:
        tasks = getattr(ctx.app, "tasks", None)
        replace = getattr(tasks, "replace", None)
        session_id = str(ctx.app.extra.get("session_id") or getattr(ctx.app, "model", "session"))
        todo_meta = self._todo_meta(args.todos)
        if callable(replace):
            try:
                await replace(session_id, args.todos)
                return ToolResult.ok(
                    f"Updated {len(args.todos)} todos.",
                    persisted=True,
                    task_store=True,
                    todos=todo_meta,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to file persistence
                from autoverse.types.events import ErrorPayload, make_event

                await ctx.events.emit(
                    make_event(
                        ErrorPayload(
                            message=f"Failed to persist todos via TaskStore: {exc}",
                            error_class="io_error",
                            recoverable=True,
                            phase="tool",
                        )
                    )
                )

        path = self._todo_path(ctx)
        payload: list[dict[str, Any]] = [t.model_dump() for t in args.todos]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            # Report through events; do not raise.
            from autoverse.types.events import ErrorPayload, make_event

            await ctx.events.emit(
                make_event(
                    ErrorPayload(
                        message=f"Failed to persist todos: {exc}",
                        error_class="io_error",
                        recoverable=True,
                        phase="tool",
                    )
                )
            )
            return ToolResult.ok(
                f"Updated {len(payload)} todos (in-memory; persistence failed).",
                persisted=False,
                todos=todo_meta,
            )
        return ToolResult.ok(f"Updated {len(payload)} todos.", persisted=True, todos=todo_meta)

    @staticmethod
    def _todo_path(ctx: ToolContext) -> Any:
        from pathlib import Path

        session_id = str(ctx.app.extra.get("session_id") or getattr(ctx.app, "model", "session"))
        return Path(ctx.cwd) / ".autoverse" / f"todos-{session_id}.json"


__all__ = ["ThinkTool", "TodoWriteTool", "TodoItem"]
