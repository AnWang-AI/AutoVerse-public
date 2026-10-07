"""Unit: EngineAdapter stamps session-rail Phase-1 meta at turn end (§9.1/§9.4).

A real Echo-backed engine over a tmp JSONLStore runs one turn; afterwards the
session's ``SessionMeta`` must carry ``last_activity_at`` + ``last_outcome`` (the
rail's unseen/completed derivation) and a lazily filled ``title`` from the first
prompt. These are the core companion writes the session rail's StoreSessionQuery
reads back (it never invents them itself, §5.6.7).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from autoverse.app.builder import EngineBuilder
from autoverse.app.options import EngineOptions
from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.infra.persist.session_catalog import SQLiteSessionCatalog
from autoverse.ports import SessionMeta, SessionProjection
from autoverse.types import Message, TextBlock


async def _run_one_turn(engine, prompt: str) -> None:
    async for _ in engine.run(prompt):
        pass


async def test_turn_end_stamps_activity_and_title(tmp_path: Path) -> None:
    store = JSONLStore(tmp_path)
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive", store=store)
    ).build()
    sid = engine.session_id

    await _run_one_turn(engine, "Refactor the auth layer please")

    meta = await store.load_meta(sid)
    assert meta.title == "Refactor the auth layer please"
    assert float(meta.extra.get("last_activity_at", 0)) > 0
    assert meta.extra.get("last_outcome") == "ok"
    # updated_at is bumped to the reply time so the rail sorts/shows last-reply.
    assert meta.updated_at >= float(meta.extra["last_activity_at"]) - 0.01
    assert meta.updated_at > meta.created_at - 0.01


async def test_title_not_overwritten_on_second_turn(tmp_path: Path) -> None:
    store = JSONLStore(tmp_path)
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive", store=store)
    ).build()
    sid = engine.session_id

    await _run_one_turn(engine, "first prompt wins the title")
    await _run_one_turn(engine, "second prompt must not overwrite")

    meta = await store.load_meta(sid)
    assert meta.title == "first prompt wins the title"


async def test_title_locked_respected_after_external_rename(tmp_path: Path) -> None:
    """If the other frontend manually renames (sets ``title_locked``), this
    engine's ``_refresh_title`` must NOT overwrite it — it re-reads meta from
    disk and checks the flag."""
    store = JSONLStore(tmp_path)
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive", store=store)
    ).build()
    sid = engine.session_id

    # First turn sets an auto title
    await _run_one_turn(engine, "original prompt")
    meta = await store.load_meta(sid)
    assert meta.title == "original prompt"

    # Simulate the other frontend renaming + locking
    await store.update_meta(sid, title="My Manual Title", title_locked=True)

    # This engine runs another turn — title must NOT be overwritten
    await _run_one_turn(engine, "another prompt")
    meta = await store.load_meta(sid)
    assert meta.title == "My Manual Title"
    assert meta.extra.get("title_locked") is True


async def test_cancel_title_refresh_waits_for_stale_task_to_stop(tmp_path: Path) -> None:
    store = JSONLStore(tmp_path)
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive", store=store)
    ).build()
    started = asyncio.Event()

    async def stale_refresh() -> None:
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(stale_refresh())
    engine._title_task = task  # type: ignore[attr-defined]
    await started.wait()

    await engine._cancel_title_refresh()  # type: ignore[attr-defined]

    assert task.cancelled()
    assert engine._title_task is None  # type: ignore[attr-defined]


async def test_cancel_pending_jobs_settles_every_background_task(tmp_path: Path) -> None:
    store = JSONLStore(tmp_path)
    engine = await EngineBuilder(
        EngineOptions(model="echo", cwd=tmp_path, session_mode="interactive", store=store)
    ).build()
    cancelled: list[str] = []

    class Jobs:
        def list(self):
            return [
                type("Job", (), {"job_id": "job-1"})(),
                type("Job", (), {"job_id": "job-2"})(),
            ]

        async def cancel(self, job_id: str) -> bool:
            cancelled.append(job_id)
            return True

    engine.app.jobs = Jobs()  # type: ignore[assignment]

    await engine._cancel_pending_jobs()  # type: ignore[attr-defined]

    assert cancelled == ["job-1", "job-2"]


async def test_bounded_activation_seed_does_not_shrink_catalog_count(
    tmp_path: Path,
) -> None:
    store = JSONLStore(tmp_path / "sessions")
    catalog = SQLiteSessionCatalog(tmp_path / "catalog.sqlite3")
    sid = "bounded-seed"
    await store.create(sid, SessionMeta(session_id=sid, cwd=str(tmp_path)))
    tail = (
        Message(role="user", content=(TextBlock("latest"),)),
        Message(role="assistant", content=(TextBlock("answer"),)),
    )
    await store.append(sid, *tail)
    await catalog.upsert_projection(
        SessionProjection(session_id=sid, message_count=12, cwd=str(tmp_path))
    )

    engine = await EngineBuilder(
        EngineOptions(
            model="echo",
            cwd=tmp_path,
            session_id=sid,
            store=store,
            session_catalog=catalog,
            initial_messages=tail,
            initial_messages_complete=False,
        )
    ).build()

    summary = await catalog.get(sid)
    assert summary is not None
    assert summary.message_count == 12
    await engine.aclose("test")
