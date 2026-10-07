"""Job table fan-in, resource-lock ordering, process reaping (docs/reference/subagents.md §6/§7.4)."""

from __future__ import annotations

import asyncio
import os

import pytest

from autoverse.core.subagent.jobs import (
    InMemoryJobStore,
    JobStatus,
    ResourceLock,
    reap_process_group,
)
from autoverse.types.abort import AbortController


async def test_S5_fan_in_collects_finished_once() -> None:
    store = InMemoryJobStore()
    j1 = await store.create("a")
    j2 = await store.create("b")
    j3 = await store.create("c")
    await store.complete(j1, "done-a")
    await store.fail(j2, "boom-b")

    first = await store.collect_finished(block=True, timeout=0.5)
    ids = {r.job_id for r in first}
    assert ids == {j1, j2}
    # collected jobs do not reappear; j3 still pending
    again = await store.collect_finished(block=False)
    assert again == []
    assert store.has_pending() is True

    await store.complete(j3, "done-c")
    third = await store.collect_finished(block=True, timeout=0.5)
    assert {r.job_id for r in third} == {j3}
    assert store.get(j1) is not None and store.get(j1).status == JobStatus.COMPLETED


async def test_S7_resource_lock_sorted_acquisition_no_deadlock() -> None:
    lock = ResourceLock()
    order: list[str] = []

    async def worker(name: str, resources: list[str]) -> None:
        async with lock.acquire(resources):
            order.append(f"{name}:enter")
            await asyncio.sleep(0.01)
            order.append(f"{name}:exit")

    # Two workers request the same two resources in OPPOSITE textual order;
    # sorted acquisition prevents the classic deadlock.
    await asyncio.wait_for(
        asyncio.gather(
            worker("w1", ["db", "port"]),
            worker("w2", ["port", "db"]),
        ),
        timeout=2.0,
    )
    # Both completed (no deadlock) and held mutually exclusively.
    assert order.count("w1:enter") == 1 and order.count("w2:enter") == 1
    # mutual exclusion: one fully finishes before the other enters
    assert order[:2] in (["w1:enter", "w1:exit"], ["w2:enter", "w2:exit"])


async def test_resource_lock_abort_interrupts_wait() -> None:
    lock = ResourceLock()
    ctrl = AbortController()
    # hold the resource so the second acquire blocks
    async with lock.acquire(["x"]):
        async def waiter() -> str:
            try:
                async with lock.acquire(["x"], ctrl.signal):
                    return "acquired"
            except Exception as e:  # AbortError
                return type(e).__name__

        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.02)
        ctrl.abort("cancel")
        result = await asyncio.wait_for(task, timeout=1.0)
    assert result == "AbortError"


async def test_resource_lock_abort_releases_underlying_lock() -> None:
    # §6.2 P5 / abort race: when an acquire is interrupted by abort, the
    # underlying asyncio.Lock must NOT be left held — a later acquire of the
    # same resource must succeed. Guards the release window in jobs.py.
    lock = ResourceLock()
    ctrl = AbortController()

    async def waiter() -> str:
        try:
            async with lock.acquire(["x"], ctrl.signal):
                return "acquired"
        except Exception as e:  # AbortError
            return type(e).__name__

    holder_released = asyncio.Event()

    async def holder() -> None:
        async with lock.acquire(["x"]):
            task = asyncio.ensure_future(waiter())
            await asyncio.sleep(0.02)  # let the waiter block on the lock
            ctrl.abort("cancel")
            assert await asyncio.wait_for(task, timeout=1.0) == "AbortError"
        holder_released.set()

    await asyncio.wait_for(holder(), timeout=2.0)
    await holder_released.wait()
    # The underlying lock must be free now — re-acquire must not hang.
    async with asyncio.timeout(1.0):
        async with lock.acquire(["x"]):
            pass


def test_S9_reap_process_group_kills_real_group() -> None:
    # Spawn a child in its own process group, then reap it.
    import subprocess

    proc = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    killed = reap_process_group([pgid])
    assert pgid in killed
    proc.wait(timeout=5)
    assert proc.returncode is not None


def test_reap_missing_group_is_silent() -> None:
    assert reap_process_group([999999999]) == []
