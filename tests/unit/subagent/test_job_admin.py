"""Job management tools + InMemoryJobStore list/cancel (docs/reference/subagents.md §6.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import autoverse.core.tools.builtin  # noqa: F401 — trigger registration
from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.subagent.jobs import InMemoryJobStore, JobStatus
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import ToolRegistry
from autoverse.core.tools.tracker import FileTracker
from autoverse.infra.tools.job_admin import JobGetTool, JobListTool, JobStopTool
from autoverse.types.abort import AbortController


class _Sink:
    async def emit(self, event: Any) -> None: ...


class _Perms:
    async def check(self, c: Any) -> Any: ...
    def check_sync(self, c: Any) -> Any: ...
    async def request_approval(self, c: Any) -> Any: ...
    def cancel_all(self) -> None: ...


class _Store:
    async def create(self, *a: Any, **k: Any) -> None: ...
    async def load_meta(self, *a: Any, **k: Any) -> Any: ...


def _ctx(jobs: Any) -> ToolContext:
    ctrl = AbortController()
    app = AppContext(
        config=Config(),
        store=_Store(),
        cwd=Path.cwd(),
        model="m",
        events=_Sink(),
        tools=ToolRegistry.build(),
        jobs=jobs,
    )
    return ToolContext(
        cwd=Path.cwd(),
        permissions=_Perms(),
        tracker=FileTracker(),
        signal=ctrl.signal,
        events=app.events,
        app=app,
    )


# ── InMemoryJobStore.list / cancel ──────────────────────────────────────────


async def test_store_list_returns_all_jobs() -> None:
    store = InMemoryJobStore()
    a = await store.create("scout: find x")
    b = await store.create("builder: do y")
    await store.complete(a, "found")
    rows = store.list()
    assert {r.job_id for r in rows} == {a, b}
    by_id = {r.job_id: r for r in rows}
    assert by_id[a].status is JobStatus.COMPLETED
    assert by_id[b].status is JobStatus.IN_PROGRESS


async def test_store_cancel_marks_terminal_and_unblocks_fanin() -> None:
    store = InMemoryJobStore()
    job = await store.create("scout: slow")
    assert store.has_pending() is True
    ok = await store.cancel(job)
    assert ok is True
    assert store.get(job).status is JobStatus.CANCELLED
    assert store.has_pending() is False
    # cancelling again (now terminal) is a no-op
    assert await store.cancel(job) is False
    # unknown id
    assert await store.cancel("nope") is False


async def test_store_cancel_cancels_bound_task() -> None:
    store = InMemoryJobStore()
    job = await store.create("builder: loop")

    async def _never() -> None:
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never())
    store.register_task(job, task)
    await store.cancel(job)
    # give the loop a tick to deliver the cancellation
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert store.get(job).status is JobStatus.CANCELLED


async def test_store_cancel_waits_for_bound_task_cleanup() -> None:
    store = InMemoryJobStore()
    job = await store.create("builder: cleanup")
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def _worker() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(_worker())
    store.register_task(job, task)
    await started.wait()

    await store.cancel(job)

    assert cleaned.is_set()
    assert task.done()
    assert store.get(job).status is JobStatus.CANCELLED


# ── JobList / JobGet / JobStop tools ────────────────────────────────────────


async def test_job_list_tool_reports_status_and_total() -> None:
    store = InMemoryJobStore()
    a = await store.create("scout: find x")
    await store.create("builder: do y")
    await store.complete(a, "found it")
    res = await JobListTool().run(JobListTool.Args(), _ctx(store))
    assert not res.is_error
    assert "scout: find x" in res.output and "builder: do y" in res.output
    assert res.metadata["total"] == 2
    assert res.metadata["pending"] == 1


async def test_job_list_tool_empty() -> None:
    res = await JobListTool().run(JobListTool.Args(), _ctx(InMemoryJobStore()))
    assert not res.is_error and "No background jobs" in res.output


async def test_job_get_tool_returns_summary_or_unknown() -> None:
    store = InMemoryJobStore()
    job = await store.create("scout: find x")
    await store.complete(job, "the answer is 42")
    res = await JobGetTool().run(JobGetTool.Args(job_id=job), _ctx(store))
    assert not res.is_error and "the answer is 42" in res.output
    assert res.metadata["status"] == "completed"

    miss = await JobGetTool().run(JobGetTool.Args(job_id="job-999"), _ctx(store))
    assert miss.is_error and "Unknown job" in miss.output


async def test_job_stop_tool_cancels_running_job() -> None:
    store = InMemoryJobStore()
    job = await store.create("builder: loop")
    res = await JobStopTool().run(JobStopTool.Args(job_id=job), _ctx(store))
    assert not res.is_error and res.metadata["cancelled"] is True
    assert store.get(job).status is JobStatus.CANCELLED

    # stopping a finished job is a graceful no-op
    again = await JobStopTool().run(JobStopTool.Args(job_id=job), _ctx(store))
    assert not again.is_error and again.metadata["cancelled"] is False


async def test_tools_error_when_no_job_store() -> None:
    ctx = _ctx(None)
    for tool, args in (
        (JobListTool(), JobListTool.Args()),
        (JobGetTool(), JobGetTool.Args(job_id="job-1")),
        (JobStopTool(), JobStopTool.Args(job_id="job-1")),
    ):
        res = await tool.run(args, ctx)
        assert res.is_error and res.error_kind == "no_job_store"
