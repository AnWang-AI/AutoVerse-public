"""Unit: FileTracker invariants and locks."""

from __future__ import annotations

import asyncio

from autoverse.core.tools.tracker import FileTracker


def test_unread_write_blocked() -> None:
    tr = FileTracker()
    from pathlib import Path

    p = Path("/x/y.txt")
    ok, reason = tr.was_read_before_write(p, 100.0)
    assert ok is False and "not been read" in reason


def test_read_then_write_ok() -> None:
    tr = FileTracker()
    from pathlib import Path

    p = Path("/x/y.txt")
    tr.set_read_mtime(p, 100.0)
    ok, _ = tr.was_read_before_write(p, 100.0)
    assert ok is True


def test_stale_read_blocked() -> None:
    tr = FileTracker()
    from pathlib import Path

    p = Path("/x/y.txt")
    tr.set_read_mtime(p, 100.0)
    ok, reason = tr.was_read_before_write(p, 200.0)
    assert ok is False and "changed on disk" in reason


async def test_file_lock_serializes_and_gcs() -> None:
    tr = FileTracker()
    from pathlib import Path

    p = Path("/x/y.txt")
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with tr.file_lock(p):
            order.append(f"{tag}-in")
            await asyncio.sleep(0.01)
            order.append(f"{tag}-out")

    await asyncio.gather(worker("a"), worker("b"))
    # Each critical section is non-overlapping.
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    )
    # Lock GC'd after release.
    assert str(p) not in tr._locks
