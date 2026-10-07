"""AgentTool — the infra thin shell over SubAgentRunner.spawn (docs/reference/subagents.md §3.3).

This tool contains ZERO orchestration logic: no isolation, no parallelism, no
audit, no locking (those all live in :class:`SubAgentRunner`, P2). It only:
declares ``Args = SpawnRequest`` (the single-source schema, P1), forwards to
``runner.spawn(...)``, and translates the :class:`SpawnResult` into a
:class:`ToolResult`.

It is NOT ``@register_tool``-decorated; the app layer registers it at runtime via
``registry.register("subagent", [AgentTool(runner)])``. ``infra → core`` is a
legal dependency direction (P3).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from autoverse.core.subagent.runner import SpawnRequest, SubAgentRunner
from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class AgentTool(BaseTool):
    name: ClassVar[str] = "agent"
    description: ClassVar[str] = (
        "Dispatch a self-contained subtask to a specialized sub-agent. "
        "ALWAYS pass subagent_type explicitly (scout/architect/builder/validator/"
        "reviewer/e2e-verifier/debugger/researcher/generalist). "
        "ALWAYS pass a short (3-5 words) description summarizing the task (shown as "
        "the agent's card label); put the full instructions in task. "
        "For worktree isolation in a repo different from the current session cwd, "
        "ALWAYS pass target_cwd pointing at that repo. "
        "Set run_in_background=true for independent parallel tasks, then collect "
        "results with JobOutput()."
    )
    # The shell itself touches no resource directly; the dispatched sub-agent's
    # real capability is bounded by its own max_tier.
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = SpawnRequest

    def __init__(self, runner: SubAgentRunner) -> None:
        self._runner = runner

    async def run(self, args: SpawnRequest, ctx: ToolContext) -> ToolResult:
        result = await self._runner.spawn(args, ctx)
        # The "{Type} Task — {description}" card label (MiMo `Task` parity): prefer
        # the explicit short ``description`` the caller passed, else fall back to the
        # first non-empty task line. Rendering concern only.
        description = (args.description or "").strip() or next(
            (ln.strip() for ln in args.task.splitlines() if ln.strip()), ""
        )
        return ToolResult(
            output=result.summary,
            is_error=(result.verdict == "FAIL" or result.tainted),
            metadata={
                "subagent_type": result.subagent_type,
                "description": description,
                "verdict": result.verdict,
                "tainted": result.tainted,
                "degraded": result.degraded,
                "trace_id": result.trace_id,
                "worktree_commit": result.worktree_commit,
                "worktree_branch": result.worktree_branch,
                "worktree_path": result.worktree_path,
                "tool_count": result.tool_count,
            },
        )


__all__ = ["AgentTool"]
