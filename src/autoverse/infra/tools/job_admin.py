"""Job management tools — JobList / JobGet / JobStop (docs/reference/subagents.md §6.2).

Thin shells over the in-process Job store (``ctx.app.jobs``). They carry ZERO
orchestration logic (that lives in :class:`~autoverse.core.subagent.runner.SubAgentRunner`):
they only read the Job table (``JobList`` / ``JobGet``) or request a cancel
(``JobStop``). Registered at runtime by the app layer under the ``subagent``
source alongside :class:`AgentTool` / :class:`JobOutputTool`; they are NOT
``@register_tool`` builtins and — because they are absent from the sub-agent
fail-closed allow-list (``filter._KNOWN_BUILTINS``) — are automatically hidden
from sub-agents, so only the main orchestrator can manage background jobs.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


def _jobs(ctx: ToolContext) -> Any:
    """Return the configured job store, or None when background jobs are off."""
    return getattr(ctx.app, "jobs", None)


def _status_of(rec: Any) -> str:
    status = getattr(rec, "status", None)
    return getattr(status, "value", str(status) if status is not None else "unknown")


def _fmt_elapsed(rec: Any) -> str:
    ms = float(getattr(rec, "elapsed_ms", 0.0) or 0.0)
    s = ms / 1000.0
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


def _line(rec: Any) -> str:
    result = getattr(rec, "result", None)
    branch = getattr(result, "worktree_branch", None)
    suffix = f" · {branch}" if branch else ""
    return f"[{rec.job_id}] {_status_of(rec)} · {_fmt_elapsed(rec)}{suffix} — {rec.subject}"


def _job_meta(rec: Any) -> dict[str, str]:
    result = getattr(rec, "result", None)
    return {
        "job_id": str(rec.job_id),
        "subject": str(rec.subject),
        "status": _status_of(rec),
        "trace_id": str(getattr(result, "trace_id", "") or ""),
        "worktree_branch": str(getattr(result, "worktree_branch", "") or ""),
        "worktree_commit": str(getattr(result, "worktree_commit", "") or ""),
        "worktree_path": str(getattr(result, "worktree_path", "") or ""),
    }


class _NoArgs(BaseModel):
    """No parameters."""


class JobListTool(BaseTool):
    name: ClassVar[str] = "JobList"
    description: ClassVar[str] = (
        "List all background sub-agent jobs (dispatched with "
        "agent(run_in_background=true)) with their status and elapsed time. "
        "Use to see what is running before collecting with JobOutput() or "
        "cancelling with JobStop()."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _NoArgs

    async def run(self, args: _NoArgs, ctx: ToolContext) -> ToolResult:
        jobs = _jobs(ctx)
        lister = getattr(jobs, "list", None)
        if jobs is None or not callable(lister):
            return ToolResult.error(
                "No job store configured; background jobs are unavailable.",
                error_kind="no_job_store",
            )
        records = list(lister())
        if not records:
            return ToolResult.ok("No background jobs.", total=0, jobs=[])
        body = "\n".join(_line(r) for r in records)
        pending = sum(1 for r in records if _status_of(r) in ("pending", "in_progress"))
        jobs_meta = [_job_meta(r) for r in records]
        return ToolResult.ok(body, total=len(records), pending=pending, jobs=jobs_meta)


class _JobIdArgs(BaseModel):
    job_id: str = Field(..., description="The job id (e.g. 'job-1') to act on.")


class JobGetTool(BaseTool):
    name: ClassVar[str] = "JobGet"
    description: ClassVar[str] = (
        "Get the full status and output/error of a single background sub-agent "
        "job by id. Does not mark the job collected (unlike JobOutput)."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _JobIdArgs

    async def run(self, args: _JobIdArgs, ctx: ToolContext) -> ToolResult:
        jobs = _jobs(ctx)
        getter = getattr(jobs, "get", None)
        if jobs is None or not callable(getter):
            return ToolResult.error(
                "No job store configured; background jobs are unavailable.",
                error_kind="no_job_store",
            )
        rec = getter(args.job_id)
        if rec is None:
            return ToolResult.error(
                f"Unknown job id {args.job_id!r}.", error_kind="unknown_job"
            )
        status = _status_of(rec)
        body = getattr(rec, "summary", "") or getattr(rec, "error", "") or "(no output yet)"
        meta = _job_meta(rec)
        return ToolResult.ok(
            f"[{rec.job_id}] {status} · {_fmt_elapsed(rec)} — {rec.subject}\n{body}",
            **meta,
        )


class JobStopTool(BaseTool):
    name: ClassVar[str] = "JobStop"
    description: ClassVar[str] = (
        "Cancel a running background sub-agent job by id. The job transitions to "
        "'cancelled' and stops consuming resources; already-finished jobs are a "
        "no-op. Collect the terminal record afterwards with JobOutput()."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _JobIdArgs

    async def run(self, args: _JobIdArgs, ctx: ToolContext) -> ToolResult:
        jobs = _jobs(ctx)
        cancel = getattr(jobs, "cancel", None)
        if jobs is None or not callable(cancel):
            return ToolResult.error(
                "No job store configured; background jobs are unavailable.",
                error_kind="no_job_store",
            )
        cancelled = await cancel(args.job_id)
        if not cancelled:
            return ToolResult.ok(
                f"Job {args.job_id!r} was not cancelled (unknown or already finished).",
                job_id=args.job_id,
                cancelled=False,
            )
        return ToolResult.ok(
            f"Cancelled job {args.job_id}.", job_id=args.job_id, cancelled=True
        )


__all__ = ["JobListTool", "JobGetTool", "JobStopTool"]
