"""Unit tests — JobStore state machine + DAG (docs/reference/events-and-storage.md §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.infra.persist.job_store import (
    CycleError,
    InvalidTransition,
    JobStatus,
    JobStore,
)


async def test_legal_transition_chain() -> None:
    store = JobStore()
    job = await store.create("s", "x")
    await store.set_status(job.id, JobStatus.IN_PROGRESS)
    await store.set_status(job.id, JobStatus.COMPLETED)
    assert store.get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]


async def test_illegal_transition_rejected() -> None:
    store = JobStore()
    job = await store.create("s", "x")
    with pytest.raises(InvalidTransition):
        await store.set_status(job.id, JobStatus.COMPLETED)  # pending → completed


async def test_terminal_is_immutable() -> None:
    store = JobStore()
    job = await store.create("s", "x")
    await store.set_status(job.id, JobStatus.CANCELLED)
    with pytest.raises(InvalidTransition):
        await store.set_status(job.id, JobStatus.IN_PROGRESS)


async def test_self_edge_rejected() -> None:
    store = JobStore()
    a = await store.create("s", "A")
    with pytest.raises(CycleError):
        await store.add_edge(a.id, a.id)


async def test_cycle_detection_multi_hop() -> None:
    store = JobStore()
    a = await store.create("s", "A")
    b = await store.create("s", "B")
    c = await store.create("s", "C")
    await store.add_edge(a.id, b.id)
    await store.add_edge(b.id, c.id)
    with pytest.raises(CycleError):
        await store.add_edge(c.id, a.id)  # would close the cycle


async def test_ready_respects_dependencies() -> None:
    store = JobStore()
    a = await store.create("s", "A")
    b = await store.create("s", "B")
    await store.add_edge(a.id, b.id)
    assert {j.id for j in store.ready("s")} == {a.id}
    await store.set_status(a.id, JobStatus.IN_PROGRESS)
    await store.complete(a.id, "done")
    assert {j.id for j in store.ready("s")} == {b.id}


async def test_collect_finished_once(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    a = await store.create("s", "A")
    await store.set_status(a.id, JobStatus.IN_PROGRESS)
    await store.complete(a.id, "ok")
    first = await store.collect_finished("s", block=False)
    assert [j.id for j in first] == [a.id]
    # Already collected → not returned again.
    assert await store.collect_finished("s", block=False) == []


async def test_load_marks_interrupted_jobs_failed(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = await store.create("s", "X")
    await store.set_status(job.id, JobStatus.IN_PROGRESS)
    # Fresh store reading the snapshot simulates a restart.
    store2 = JobStore(tmp_path)
    loaded = store2.load("s")
    assert loaded[0].status is JobStatus.FAILED
    assert "interrupted by restart" in loaded[0].output


async def test_concurrent_terminal_transition_no_lost_update() -> None:
    """Two coroutines racing to terminate an in_progress job: exactly one wins,
    the other sees the already-terminal state and raises (docs/reference/events-and-storage.md §7.2).

    Without serialising the check-then-set both would read IN_PROGRESS, both
    pass the transition guard, and the second silently overwrites the first."""
    import asyncio

    store = JobStore()
    job = await store.create("s", "x")
    await store.set_status(job.id, JobStatus.IN_PROGRESS)

    results: list[str] = []

    async def _complete() -> None:
        try:
            await store.complete(job.id, "done")
            results.append("complete")
        except InvalidTransition:
            results.append("complete-rejected")

    async def _fail() -> None:
        try:
            await store.fail(job.id, "boom")
            results.append("fail")
        except InvalidTransition:
            results.append("fail-rejected")

    await asyncio.gather(_complete(), _fail())

    # Exactly one transition took effect; the other was rejected.
    assert sorted(results) in (
        ["complete", "fail-rejected"],
        ["complete-rejected", "fail"],
    ), results
    assert store.get(job.id).status in (JobStatus.COMPLETED, JobStatus.FAILED)  # type: ignore[union-attr]


async def test_persist_holds_flock(tmp_path: Path) -> None:
    """_persist must take an exclusive cross-process lock so a concurrent
    rewriter in another process cannot clobber the snapshot mid-write."""
    import asyncio
    import fcntl
    import threading

    store = JobStore(tmp_path)
    job = await store.create("s", "x")

    lock_path = tmp_path / "s.lock"
    holder = lock_path.open("w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

    done = threading.Event()

    def _do() -> None:
        asyncio.run(_set(store, job.id))
        done.set()

    async def _set(store: JobStore, jid: str) -> None:
        await store.set_status(jid, JobStatus.IN_PROGRESS)

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    assert not done.wait(timeout=0.5)  # blocked on the foreign flock
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    assert done.wait(timeout=5.0)
    t.join(timeout=5.0)
