"""JobOutput — collect finished background sub-agent jobs (docs/reference/subagents.md §6.2, B3).

A thin shell over the job store's batch fan-in (``collect_finished``): it contains
no orchestration (that lives in :class:`SubAgentRunner`), it only reads terminal
jobs and renders them for the model. Registered at runtime by the app layer under
the ``subagent`` source alongside :class:`AgentTool`; it is intentionally NOT a
``@register_tool`` builtin and is hidden from sub-agents (fail-closed filter).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class JobOutputArgs(BaseModel):
    block: bool = Field(
        True, description="Wait until at least one background job finishes."
    )
    timeout: float | None = Field(
        None, description="Max seconds to wait when block=true (None = unbounded)."
    )


class JobOutputTool(BaseTool):
    name: ClassVar[str] = "JobOutput"
    description: ClassVar[str] = (
        "Collect results from finished background sub-agent jobs dispatched with "
        "agent(run_in_background=true). Returns terminal jobs once; still-running "
        "jobs are reported as pending and reappear on the next call."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = JobOutputArgs

    async def run(self, args: JobOutputArgs, ctx: ToolContext) -> ToolResult:
        jobs = getattr(ctx.app, "jobs", None)
        collect = getattr(jobs, "collect_finished", None)
        if jobs is None or not callable(collect):
            return ToolResult.error(
                "No job store configured; background jobs are unavailable.",
                error_kind="no_job_store",
            )
        finished = await collect(block=args.block, timeout=args.timeout)
        has_pending = bool(getattr(jobs, "has_pending", lambda: False)())
        if not finished:
            note = "still running" if has_pending else "none dispatched"
            return ToolResult.ok(
                f"No finished background jobs ({note}).", pending=has_pending
            )
        sections: list[str] = []
        jobs_meta: list[dict[str, str]] = []
        for rec in finished:
            status = getattr(getattr(rec, "status", None), "value", "completed")
            body = rec.summary or rec.error or "(no output)"
            result = getattr(rec, "result", None)
            wt_lines: list[str] = []
            branch = getattr(result, "worktree_branch", None)
            commit = getattr(result, "worktree_commit", None)
            path = getattr(result, "worktree_path", None)
            trace_id = getattr(result, "trace_id", None)
            if branch or commit or path:
                wt_lines.append("Worktree:")
                if branch:
                    wt_lines.append(f"- branch: {branch}")
                if commit:
                    wt_lines.append(f"- commit: {commit}")
                if path:
                    wt_lines.append(f"- path: {path}")
            if trace_id:
                wt_lines.append(f"trace_id: {trace_id}")
            if wt_lines:
                body = f"{body}\n\n" + "\n".join(wt_lines)
            sections.append(f"[{rec.job_id}] {rec.subject} — {status}\n{body}")
            jobs_meta.append(
                {
                    "job_id": str(rec.job_id),
                    "subject": str(rec.subject),
                    "status": status,
                    "trace_id": str(trace_id or ""),
                    "worktree_branch": str(branch or ""),
                    "worktree_commit": str(commit or ""),
                    "worktree_path": str(path or ""),
                }
            )
        return ToolResult.ok(
            "\n\n".join(sections),
            collected=len(finished),
            pending=has_pending,
            jobs=jobs_meta,
        )


__all__ = ["JobOutputArgs", "JobOutputTool"]
