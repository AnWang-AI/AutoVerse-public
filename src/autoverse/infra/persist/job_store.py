"""JobStore — background-job state machine + DAG + fan-in (docs/reference/events-and-storage.md §7).

- A status state machine guards legal transitions; illegal transitions raise
  :class:`InvalidTransition` and terminal states are immutable (D6, §7.2).
- A dependency DAG (``blocks``/``blocked_by`` edges) is maintained bidirectionally;
  ``add_edge`` rejects cycles via DFS (D7, §7.3).
- ``ready`` returns jobs whose dependencies are all completed; ``collect_finished``
  performs event-driven fan-in returning uncollected terminal jobs once (§7.4).

A JSONL snapshot stream persists job state (one snapshot per line; same id
overwrites). ``core`` purity is irrelevant here — this is an ``infra`` adapter.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path


class JobStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

_ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.IN_PROGRESS, JobStatus.CANCELLED}),
    JobStatus.IN_PROGRESS: frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    """Raised when an illegal status transition is attempted (§7.2)."""


class CycleError(Exception):
    """Raised when adding a dependency edge would create a cycle (§7.3)."""


@dataclass
class Job:
    id: str
    session_id: str
    subject: str
    description: str = ""
    status: JobStatus = JobStatus.PENDING
    owner: str = ""
    output: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    spawner_tool_use_id: str = ""
    collected: bool = False


class JobStore:
    """In-process job table with a JSONL snapshot backend (§7.5)."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self._base = Path(base_dir) if base_dir is not None else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._terminal_event = asyncio.Event()
        self._terminal_generation = 0
        # Serialises check-then-set status transitions so a concurrent
        # transition cannot read a stale status, pass the guard, and clobber a
        # peer's update (docs/reference/events-and-storage.md §7.2).
        self._mutate_lock = asyncio.Lock()

    # ── creation ─────────────────────────────────────────────────────────────

    async def create(
        self,
        session_id: str,
        subject: str,
        *,
        description: str = "",
        owner: str = "",
        spawner_tool_use_id: str = "",
        job_id: str | None = None,
    ) -> Job:
        jid = job_id or f"job-{uuid.uuid4().hex[:12]}"
        job = Job(
            id=jid,
            session_id=session_id,
            subject=subject,
            description=description,
            owner=owner,
            spawner_tool_use_id=spawner_tool_use_id,
        )
        self._jobs[jid] = job
        await self._persist(session_id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    # ── state machine ────────────────────────────────────────────────────────

    async def set_status(self, job_id: str, new: JobStatus) -> None:
        async with self._mutate_lock:
            await self._set_status_locked(job_id, new)

    async def _set_status_locked(
        self, job_id: str, new: JobStatus, *, output: str | None = None
    ) -> None:
        job = self._jobs[job_id]
        if new not in _ALLOWED[job.status]:
            raise InvalidTransition(f"{job.status.value} → {new.value} is illegal")
        if output is not None:
            job.output = output
        job.status = new
        job.updated_at = time.time()
        if new is JobStatus.IN_PROGRESS:
            job.started_at = time.time()
        await self._persist(job.session_id)
        if new in _TERMINAL:
            self._signal_terminal()

    async def complete(self, job_id: str, output: str = "") -> None:
        # Output is applied inside the lock *only if* the transition is legal,
        # so a rejected race does not leave a stray output behind.
        async with self._mutate_lock:
            await self._set_status_locked(job_id, JobStatus.COMPLETED, output=output)

    async def fail(self, job_id: str, output: str = "") -> None:
        async with self._mutate_lock:
            await self._set_status_locked(job_id, JobStatus.FAILED, output=output)

    # ── DAG ──────────────────────────────────────────────────────────────────

    async def add_edge(self, upstream: str, downstream: str) -> None:
        """Add ``upstream blocks downstream``; reject if it forms a cycle (D7)."""
        if upstream == downstream:
            raise CycleError("a job cannot block itself")
        if self._would_cycle(upstream, downstream):
            raise CycleError(f"edge {upstream} → {downstream} would create a cycle")
        up = self._jobs[upstream]
        down = self._jobs[downstream]
        if downstream not in up.blocks:
            up.blocks.append(downstream)
        if upstream not in down.blocked_by:
            down.blocked_by.append(upstream)
        await self._persist(up.session_id)

    def is_runnable(self, job: Job) -> bool:
        return all(
            self._jobs[d].status is JobStatus.COMPLETED
            for d in job.blocked_by
            if d in self._jobs
        )

    def ready(self, session_id: str) -> list[Job]:
        """Pending jobs in *session_id* whose dependencies are all completed."""
        return [
            j
            for j in self._jobs.values()
            if j.session_id == session_id
            and j.status is JobStatus.PENDING
            and self.is_runnable(j)
        ]

    # ── fan-in ───────────────────────────────────────────────────────────────

    def has_pending(self, session_id: str) -> bool:
        return any(
            j.session_id == session_id and j.status not in _TERMINAL
            for j in self._jobs.values()
        )

    async def collect_finished(
        self, session_id: str, *, block: bool = True, timeout: float | None = None
    ) -> list[Job]:
        """Return uncollected terminal jobs once; mark them collected (§7.4)."""
        while True:
            gen = self._terminal_generation
            event = self._terminal_event
            ready = [
                j
                for j in self._jobs.values()
                if j.session_id == session_id
                and j.status in _TERMINAL
                and not j.collected
            ]
            if ready:
                for j in ready:
                    j.collected = True
                await self._persist(session_id)
                return ready
            if not block or not self.has_pending(session_id):
                return []
            if gen == self._terminal_generation:
                try:
                    await asyncio.wait_for(event.wait(), timeout)
                except asyncio.TimeoutError:
                    return []

    # ── persistence ──────────────────────────────────────────────────────────

    def load(self, session_id: str) -> list[Job]:
        """Reload job snapshots from disk, marking interrupted jobs failed (§7.5)."""
        if self._base is None:
            return [j for j in self._jobs.values() if j.session_id == session_id]
        path = self._base / f"{session_id}.jsonl"
        if not path.exists():
            return []
        by_id: dict[str, Job] = {}
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                job = _job_from_dict(obj)
                if job.status is JobStatus.IN_PROGRESS:
                    job = replace(
                        job,
                        status=JobStatus.FAILED,
                        output="interrupted by restart",
                    )
                by_id[job.id] = job
        for job in by_id.values():
            self._jobs[job.id] = job
        return list(by_id.values())

    async def _persist(self, session_id: str) -> None:
        if self._base is None:
            return
        path = self._base / f"{session_id}.jsonl"
        jobs = [j for j in self._jobs.values() if j.session_id == session_id]
        body = "".join(
            json.dumps(_job_to_dict(j), ensure_ascii=False) + "\n" for j in jobs
        )
        # The snapshot is a full-file overwrite (tmp → os.replace). Hold an
        # exclusive cross-process flock around it so two processes persisting the
        # same session cannot interleave and clobber each other's state
        # (docs/reference/events-and-storage.md §9.2). The lock file is separate from the data file so
        # the os.replace inode swap never invalidates the held lock.
        lock_path = self._base / f"{session_id}.lock"
        with lock_path.open("w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                tmp = self._base / f".{session_id}.{uuid.uuid4().hex}.tmp"
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, path)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # ── internals ────────────────────────────────────────────────────────────

    def _signal_terminal(self) -> None:
        self._terminal_generation += 1
        self._terminal_event.set()
        self._terminal_event = asyncio.Event()

    def _would_cycle(self, upstream: str, downstream: str) -> bool:
        # Adding upstream -> downstream cycles iff downstream can already reach
        # upstream by following blocks edges.
        seen: set[str] = set()
        stack = [downstream]
        while stack:
            node = stack.pop()
            if node == upstream:
                return True
            if node in seen:
                continue
            seen.add(node)
            job = self._jobs.get(node)
            if job is not None:
                stack.extend(job.blocks)
        return False


def _job_to_dict(j: Job) -> dict[str, object]:
    return {
        "id": j.id,
        "session_id": j.session_id,
        "subject": j.subject,
        "description": j.description,
        "status": j.status.value,
        "owner": j.owner,
        "output": j.output,
        "created_at": j.created_at,
        "updated_at": j.updated_at,
        "started_at": j.started_at,
        "blocks": list(j.blocks),
        "blocked_by": list(j.blocked_by),
        "spawner_tool_use_id": j.spawner_tool_use_id,
        "collected": j.collected,
    }


def _num(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def _job_from_dict(d: dict[str, object]) -> Job:
    started = d.get("started_at")
    return Job(
        id=str(d["id"]),
        session_id=str(d["session_id"]),
        subject=str(d.get("subject", "")),
        description=str(d.get("description", "")),
        status=JobStatus(str(d.get("status", "pending"))),
        owner=str(d.get("owner", "")),
        output=str(d.get("output", "")),
        created_at=_num(d.get("created_at")),
        updated_at=_num(d.get("updated_at")),
        started_at=None if started is None else _num(started),
        blocks=_str_list(d.get("blocks")),
        blocked_by=_str_list(d.get("blocked_by")),
        spawner_tool_use_id=str(d.get("spawner_tool_use_id", "")),
        collected=bool(d.get("collected", False)),
    )


__all__ = ["JobStore", "Job", "JobStatus", "InvalidTransition", "CycleError"]
