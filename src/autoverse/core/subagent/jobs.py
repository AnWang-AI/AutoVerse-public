"""Job table + fan-in + resource lock + process reaping (docs/reference/subagents.md §6.2/§6.3/§7.4).

- :class:`InMemoryJobStore` tracks background sub-agent jobs and supports batch
  fan-in (``collect_finished``): terminal jobs are returned once and marked
  collected; still-running jobs reappear next call (§6.2).
- :class:`ResourceLock` acquires ``exclusive_resources`` in a globally consistent
  sorted order to prevent deadlock (§6.3).
- :func:`reap_process_group` kills a bound process group via ``os.killpg`` (§7.4).

``core`` purity: imports only ``types``/sibling ``core`` modules + stdlib.
"""

from __future__ import annotations

import asyncio
import os
import signal as _signal
import time
from collections.abc import Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, List as _List

from autoverse.types.abort import AbortSignal


class JobStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass
class JobRecord:
    job_id: str
    subject: str
    kind: str = "subagent"
    status: JobStatus = JobStatus.PENDING
    summary: str = ""
    result: Any = None
    error: str = ""
    parent_trace: str | None = None
    collected: bool = False
    created_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None

    @property
    def elapsed_ms(self) -> float:
        """Wall-clock since dispatch (frozen at terminal state)."""
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return max(0.0, (end - self.created_at) * 1000.0)


class InMemoryJobStore:
    """In-process job table with batch fan-in; not a persistent store."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._seq = 0
        self._finished = asyncio.Event()
        #: job_id → the asyncio task running the background coro, so JobStop can
        #: cancel it. Registered by the dispatcher; absent for already-terminal
        #: or never-launched jobs.
        self._tasks: dict[str, "asyncio.Task[Any]"] = {}

    async def create(
        self,
        subject: str,
        *,
        kind: str = "subagent",
        parent_trace: str | None = None,
    ) -> str:
        self._seq += 1
        job_id = f"job-{self._seq}"
        self._jobs[job_id] = JobRecord(
            job_id=job_id,
            subject=subject,
            kind=kind,
            status=JobStatus.IN_PROGRESS,
            parent_trace=parent_trace,
        )
        return job_id

    async def complete(self, job_id: str, summary: str, result: Any = None) -> None:
        rec = self._jobs[job_id]
        if rec.status in _TERMINAL:
            return  # terminal is immutable (P5): a late complete must not reopen
        rec.status = JobStatus.COMPLETED
        rec.summary = summary
        rec.result = result
        rec.ended_at = time.monotonic()
        self._tasks.pop(job_id, None)
        self._finished.set()

    async def fail(self, job_id: str, error: str) -> None:
        rec = self._jobs[job_id]
        if rec.status in _TERMINAL:
            return
        rec.status = JobStatus.FAILED
        rec.error = error
        rec.ended_at = time.monotonic()
        self._tasks.pop(job_id, None)
        self._finished.set()

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def list(self) -> list[JobRecord]:
        """Snapshot of every job, newest-created last (insertion order)."""
        return list(self._jobs.values())

    def register_task(self, job_id: str, task: "asyncio.Task[Any]") -> None:
        """Bind the running asyncio task so :meth:`cancel` can stop it (§6.2)."""
        if job_id in self._jobs and self._jobs[job_id].status not in _TERMINAL:
            self._tasks[job_id] = task

    async def cancel(self, job_id: str) -> bool:
        """Cancel a running background job (JobStop, §6.2).

        Cancels the bound asyncio task (the ``except BaseException`` guard in the
        dispatcher then records the terminal state) and, if no task is bound or
        the job is still non-terminal afterwards, marks it CANCELLED directly so
        fan-in never hangs. Returns False if the job is unknown/already terminal.
        """
        rec = self._jobs.get(job_id)
        if rec is None or rec.status in _TERMINAL:
            return False
        task = self._tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
            # A rewind may restore the workspace immediately after cancellation.
            # Wait until the coroutine's finally blocks have completed so the
            # discarded job cannot write into the restored tree afterwards.
            await asyncio.gather(task, return_exceptions=True)
        if rec.status not in _TERMINAL:
            rec.status = JobStatus.CANCELLED
            rec.error = rec.error or "cancelled by JobStop"
            rec.ended_at = time.monotonic()
        self._finished.set()
        return True

    def has_pending(self) -> bool:
        return any(j.status not in _TERMINAL for j in self._jobs.values())

    async def collect_finished(
        self, *, block: bool = True, timeout: float | None = None
    ) -> _List[JobRecord]:
        """Batch fan-in (§6.2): return terminal-but-uncollected jobs.

        Terminal jobs are marked collected so they are not returned again.
        Still-running jobs are not returned (they reappear next call). When
        ``block`` is True and nothing is ready, wait until at least one finishes.
        """
        if block and not self._ready_count():
            if self.has_pending():
                self._finished.clear()
                try:
                    await asyncio.wait_for(self._finished.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
        out: list[JobRecord] = []
        for rec in self._jobs.values():
            if rec.status in _TERMINAL and not rec.collected:
                rec.collected = True
                out.append(rec)
        return out

    def _ready_count(self) -> int:
        return sum(
            1 for j in self._jobs.values() if j.status in _TERMINAL and not j.collected
        )


class ResourceLock:
    """Mutex over named resources with sorted acquisition (deadlock prevention, §6.3).

    All sub-agents acquire a resource set in the same global (sorted) order,
    eliminating circular waits (the classic dining-philosophers solution).
    Waiting is interruptible via ``AbortSignal``.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, resource: str) -> asyncio.Lock:
        lock = self._locks.get(resource)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[resource] = lock
        return lock

    @asynccontextmanager
    async def acquire(
        self, resources: Iterable[str], signal: AbortSignal | None = None
    ) -> AsyncIterator[None]:
        ordered: Sequence[str] = sorted(set(resources))
        held: list[asyncio.Lock] = []
        try:
            for resource in ordered:
                lock = self._lock_for(resource)
                if signal is not None:
                    acquire_task = asyncio.ensure_future(lock.acquire())
                    abort_task = asyncio.ensure_future(signal.wait())
                    done, pending = await asyncio.wait(
                        {acquire_task, abort_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if acquire_task in done:
                        abort_task.cancel()
                        held.append(lock)
                    else:
                        # Aborted while waiting. Settle the acquire deterministically
                        # so the underlying Lock is never left held (no release
                        # window): cancel it, then await. If it nonetheless acquired
                        # the lock before the cancel landed, we now own it and must
                        # release it; otherwise awaiting raises CancelledError and the
                        # Lock already released itself.
                        acquire_task.cancel()
                        try:
                            await acquire_task
                        except asyncio.CancelledError:
                            pass
                        else:
                            lock.release()
                        signal.raise_if_aborted()
                else:
                    await lock.acquire()
                    held.append(lock)
            yield
        finally:
            for lock in reversed(held):
                if lock.locked():
                    lock.release()


def reap_process_group(pgids: Iterable[int]) -> list[int]:
    """Kill bound process groups via ``os.killpg`` (§7.4). Returns killed pgids.

    Best-effort: a pgid that no longer exists is skipped silently. Independent
    process groups (``start_new_session=True`` at spawn) keep this from hitting
    the main REPL.
    """
    killed: list[int] = []
    for pgid in pgids:
        try:
            os.killpg(pgid, _signal.SIGKILL)
            killed.append(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return killed


__all__ = [
    "JobStatus",
    "JobRecord",
    "InMemoryJobStore",
    "ResourceLock",
    "reap_process_group",
]
