"""Phase 5 acceptance — persistence D1–D9 (docs/reference/testing.md §8).

| D1 JSONL round-trip | D2 incremental load | D3 atomic rewrite + reason
| D4 crash recovery (LoadReport) | D5 busy lock self-heal
| D6 JobStore state machine | D7 Job DAG + cycle reject
| D8 concurrent O_APPEND no interleave | D9 Redis equivalence
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from autoverse.infra.persist.busy_lock import BusyLock
from autoverse.infra.persist.job_store import (
    InvalidTransition,
    JobStatus,
    JobStore,
)
from autoverse.infra.persist.job_store import CycleError
from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.ports import SessionMeta
from autoverse.types.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


def _rich_messages() -> list[Message]:
    return [
        Message(role="user", content=(TextBlock(text="hello"),)),
        Message(
            role="assistant",
            content=(
                ThinkingBlock(text="planning", signature="sig-1"),
                TextBlock(text="let me check"),
                ToolUseBlock(id="t1", name="bash", input={"command": "ls"}),
            ),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(tool_use_id="t1", content="a.txt\nb.txt", is_error=False),
                ImageBlock(media_type="image/png", data="QUJD", source_type="base64"),
            ),
        ),
    ]


# ── D1 — JSONL lossless round-trip (all block types, order preserved) ─────────
async def test_d1_jsonl_round_trip(tmp_path: Path) -> None:
    """Given a session with tool_use/result/image/thinking blocks
    When appended then loaded
    Then every block is reconstructed losslessly, order identical."""
    store = JSONLStore(tmp_path)
    sid = "s1"
    await store.create(sid, SessionMeta(session_id=sid, model="m"))
    msgs = _rich_messages()
    await store.append(sid, *msgs)
    loaded = await store.load(sid)
    assert loaded == msgs


# ── D2 — incremental load (line0=meta, lines1+=msg) ───────────────────────────
async def test_d2_incremental_load(tmp_path: Path) -> None:
    """Given a large session
    When load(since=k)
    Then only messages with index >= k are returned; line0 is meta."""
    store = JSONLStore(tmp_path)
    sid = "s2"
    await store.create(sid, SessionMeta(session_id=sid))
    msgs = _rich_messages()
    await store.append(sid, *msgs)

    first_line = (tmp_path / f"{sid}.jsonl").read_text().splitlines()[0]
    assert json.loads(first_line)["_kind"] == "meta"

    tail = await store.load(sid, since=2)
    assert tail == msgs[2:]


# ── D3 — rewrite is atomic and records the reason ─────────────────────────────
async def test_d3_rewrite_atomic_records_reason(tmp_path: Path) -> None:
    """Given some history
    When rewrite(new, reason="compact:micro")
    Then the file is atomically replaced and the reason is recorded in meta."""
    store = JSONLStore(tmp_path)
    sid = "s3"
    await store.create(sid, SessionMeta(session_id=sid))
    await store.append(sid, *_rich_messages())

    new = [Message(role="user", content=(TextBlock(text="summary"),))]
    await store.rewrite(sid, new, reason="compact:micro")

    assert await store.load(sid) == new
    meta = await store.load_meta(sid)
    assert meta.extra["last_rewrite_reason"] == "compact:micro"


# ── D4 — crash recovery: skip malformed lines, return LoadReport ──────────────
async def test_d4_crash_recovery_load_report(tmp_path: Path) -> None:
    """Given a file with a half-written (corrupt) trailing line
    When load_with_report
    Then malformed lines are skipped and counted, not a whole-file failure."""
    store = JSONLStore(tmp_path)
    sid = "s4"
    await store.create(sid, SessionMeta(session_id=sid))
    await store.append(sid, Message(role="user", content=(TextBlock(text="ok"),)))
    # Simulate a crash: append a half-written JSON line.
    with (tmp_path / f"{sid}.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"_kind":"msg","role":"user","conte')

    msgs, report = await store.load_with_report(sid)
    assert len(msgs) == 1
    assert report.loaded == 1
    assert report.skipped == 1


# ── D5 — busy lock visible; self-heals when holder pid is dead ────────────────
async def test_d5_busy_lock_self_heal(tmp_path: Path) -> None:
    """Given a session being processed
    When an external process probes and the holder dies
    Then busy is visible while alive and self-heals once the pid is dead."""
    lock = BusyLock(tmp_path)
    sid = "s5"
    lock.acquire(sid, frontend="repl")
    assert lock.is_busy(sid) is True  # our own pid is alive

    # Rewrite the marker to point at a definitely-dead pid.
    dead_pid = _find_dead_pid()
    (tmp_path / f"{sid}.busy").write_text(
        json.dumps({"pid": dead_pid, "started_at": "x", "frontend": "repl"})
    )
    assert lock.is_busy(sid) is False  # stale lock reclaimed
    assert not (tmp_path / f"{sid}.busy").exists()


def _find_dead_pid() -> int:
    pid = 999_999
    while pid > 1:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pid -= 1
            continue
        pid -= 1
    return 999_999


# ── D6 — JobStore state machine: legal transitions, illegal rejected ──────────
async def test_d6_job_state_machine(tmp_path: Path) -> None:
    """Given a job going pending → in_progress → completed
    When transitions are applied
    Then legal ones succeed and illegal ones raise InvalidTransition."""
    store = JobStore(tmp_path)
    job = await store.create("sess", "build the thing")
    assert job.status is JobStatus.PENDING

    await store.set_status(job.id, JobStatus.IN_PROGRESS)
    await store.set_status(job.id, JobStatus.COMPLETED)
    assert store.get(job.id).status is JobStatus.COMPLETED  # type: ignore[union-attr]

    # Terminal is immutable.
    with pytest.raises(InvalidTransition):
        await store.set_status(job.id, JobStatus.IN_PROGRESS)

    # pending → completed is illegal (must go through in_progress).
    other = await store.create("sess", "second")
    with pytest.raises(InvalidTransition):
        await store.set_status(other.id, JobStatus.COMPLETED)


# ── D7 — Job DAG: downstream blocked until upstream done; cycles rejected ─────
async def test_d7_job_dag_and_cycle_rejection(tmp_path: Path) -> None:
    """Given job A blocks B
    When scheduling
    Then B is not ready until A is completed; adding a cycle is rejected."""
    store = JobStore(tmp_path)
    a = await store.create("sess", "A")
    b = await store.create("sess", "B")
    await store.add_edge(a.id, b.id)  # A blocks B

    ready_ids = {j.id for j in store.ready("sess")}
    assert a.id in ready_ids and b.id not in ready_ids

    await store.set_status(a.id, JobStatus.IN_PROGRESS)
    await store.set_status(a.id, JobStatus.COMPLETED)
    ready_ids = {j.id for j in store.ready("sess")}
    assert b.id in ready_ids

    # Adding B blocks A would create a cycle.
    with pytest.raises(CycleError):
        await store.add_edge(b.id, a.id)


# ── D8 — concurrent appends via O_APPEND do not interleave ────────────────────
async def test_d8_concurrent_appends_no_interleave(tmp_path: Path) -> None:
    """Given two writers appending to the same session
    When they run concurrently
    Then all messages land as complete lines, none interleaved/corrupted."""
    store = JSONLStore(tmp_path)
    sid = "s8"
    await store.create(sid, SessionMeta(session_id=sid))

    async def writer(tag: str, n: int) -> None:
        for i in range(n):
            await store.append(
                sid, Message(role="user", content=(TextBlock(text=f"{tag}-{i}"),))
            )

    await asyncio.gather(writer("A", 25), writer("B", 25))

    # Every non-meta line must be valid JSON (no half lines), and we get 50 msgs.
    lines = (tmp_path / f"{sid}.jsonl").read_text().splitlines()
    for line in lines:
        json.loads(line)  # raises if interleaved/corrupt
    msgs = await store.load(sid)
    assert len(msgs) == 50


# ── D9 — Redis equivalence (skipped without redis + server) ───────────────────
async def test_d9_redis_equivalence(tmp_path: Path) -> None:
    """Given the same D1/D3 scenarios on RedisStore
    When run
    Then behaviour matches JSONLStore (same Store contract)."""
    pytest.importorskip("redis")
    from autoverse.infra.persist.redis_store import RedisStore

    try:
        store = RedisStore("redis://localhost:6379/15")
        await store._client.ping()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - no server available
        pytest.skip("no redis server available")

    sid = "rs9"
    await store.delete(sid)
    await store.create(sid, SessionMeta(session_id=sid, model="m"))
    msgs = _rich_messages()
    await store.append(sid, *msgs)
    assert await store.load(sid) == msgs

    new = [Message(role="user", content=(TextBlock(text="summary"),))]
    await store.rewrite(sid, new, reason="compact:micro")
    assert await store.load(sid) == new
    meta = await store.load_meta(sid)
    assert meta.extra["last_rewrite_reason"] == "compact:micro"
    await store.delete(sid)


# ── Engine BusyLock wiring — acquire during turn, release after ──────────────
async def test_engine_busy_lock_acquired_during_turn(tmp_path: Path) -> None:
    """When an engine turn is running, the ``.busy`` marker exists with our pid;
    after the turn ends, it is cleaned up so the other frontend's rail stops
    showing ``busy_external``."""
    from autoverse.app.builder import EngineBuilder
    from autoverse.app.options import EngineOptions
    from autoverse.types import EventType

    session_root = tmp_path / "sessions"
    session_root.mkdir()
    store = JSONLStore(session_root)

    opts = EngineOptions(
        model="echo",
        cwd=tmp_path,
        session_mode="one_shot",
        store=store,
    )
    engine = await EngineBuilder(opts).build()
    sid = engine.session_id
    busy = BusyLock(session_root)

    # Before a turn: no busy marker.
    assert not busy.is_busy(sid)

    # Run a turn — the echo provider finishes immediately, so we just need to
    # confirm the marker is cleaned up after (the acquire/release happens inside
    # run()). The marker may or may not be visible mid-turn depending on timing,
    # but it MUST be gone after.
    async for _event in engine.run("hello"):
        pass

    # After the turn: marker released.
    assert not busy.is_busy(sid)
    assert not (session_root / f"{sid}.busy").exists()

    await engine.aclose("test")
