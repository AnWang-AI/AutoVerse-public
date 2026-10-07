"""Unit: StoreSessionQuery — the read-only SessionListQuery adapter (§4.2/§5.6).

Backed by a real :class:`JSONLStore` in a tmp dir so the cheap mtime/prompt-count
path and the busy-lock probe run exactly as in production. Covers:

* list() sorts newest-activity first, counts messages, derives title/model/cwd.
* unseen + status derivation from last_activity_at vs last_viewed_at (§5.6.5),
  and error_unseen from last_outcome (§5.6).
* rename writes SessionMeta.title and invalidates the cache.
* delete removes the session and drops it from the cache.
* group_root surfaces for worktree grouping (§6.3.1).
"""

from __future__ import annotations

from autoverse.app.session_query import StoreSessionQuery
from autoverse.infra.persist.jsonl_store import JSONLStore
from autoverse.infra.persist.session_catalog import SQLiteSessionCatalog
from autoverse.ports import SessionMeta
from autoverse.types.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock


async def _seed(store: JSONLStore, sid: str, **meta: object) -> None:
    await store.create(sid, SessionMeta(session_id=sid, **meta))  # type: ignore[arg-type]


async def test_list_sorts_newest_first_and_counts(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "old", updated_at=10.0, title="Old")
    await _seed(store, "new", updated_at=20.0, title="New")
    await store.append("new", Message(role="user", content=[TextBlock("hi")]))

    q = StoreSessionQuery(store, base_dir=tmp_path)
    rows = await q.list()

    assert [r.session_id for r in rows] == ["new", "old"]
    assert rows[0].title == "New"
    assert rows[0].message_count == 1
    assert rows[1].message_count == 0


async def test_count_is_user_prompts_not_store_messages(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "s", updated_at=20.0, title="Tools")
    await store.append(
        "s",
        Message(role="user", content=[TextBlock("please inspect this")]),
        Message(role="assistant", content=[ToolUseBlock("t1", "read_file", {"file_path": "/a.py"})]),
        Message(role="user", content=[ToolResultBlock("t1", "contents")]),
    )

    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()

    assert row.message_count == 1


async def test_legacy_session_falls_back_to_file_mtime(tmp_path) -> None:
    # A pre-stamp session: no last_activity_at, updated_at == created_at (0). Its
    # "last reply" time must fall back to the file's mtime so it still shows a
    # real time and sorts correctly — no migration needed.
    import os
    import time

    store = JSONLStore(tmp_path)
    await _seed(store, "legacy")  # created_at=updated_at=0.0, no extra stamps
    await store.append("legacy", Message(role="user", content=[TextBlock("hi")]))
    touched = time.time() - 3600
    os.utime(tmp_path / "legacy.jsonl", (touched, touched))

    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()
    assert abs(row.updated_at - touched) < 1.0


async def test_viewing_a_session_does_not_reorder_it(tmp_path) -> None:
    # Regression (user report): switching INTO a session stamps last_viewed_at,
    # which rewrites the meta line and bumps the file mtime. The sort must NOT
    # treat that view-only churn as fresh activity — the *current* session should
    # keep its place, not float to the top on every switch.
    import time

    store = JSONLStore(tmp_path)
    older_t = time.time() - 7200
    newer_t = time.time() - 60
    await _seed(store, "older", created_at=older_t, updated_at=older_t,
                extra={"last_activity_at": older_t})
    await store.append("older", Message(role="user", content=[TextBlock("a")]))
    await _seed(store, "newer", created_at=newer_t, updated_at=newer_t,
                extra={"last_activity_at": newer_t})
    await store.append("newer", Message(role="user", content=[TextBlock("b")]))

    q = StoreSessionQuery(store, base_dir=tmp_path)
    assert [r.session_id for r in await q.list()] == ["newer", "older"]

    # "View" the older session → last_viewed_at stamp rewrites its file (mtime=now).
    await store.update_meta("older", last_viewed_at=time.time())

    # Order is unchanged: the view did not float "older" above "newer".
    assert [r.session_id for r in await q.list()] == ["newer", "older"]


async def test_new_session_sorts_to_top(tmp_path) -> None:
    # A freshly created session (created_at=now, 0 messages) must out-sort an
    # older session that last replied a while ago — it belongs at the top.
    import time

    store = JSONLStore(tmp_path)
    old_t = time.time() - 3600
    await _seed(store, "older", created_at=old_t, updated_at=old_t,
                extra={"last_activity_at": old_t})
    await store.append("older", Message(role="user", content=[TextBlock("hi")]))
    now = time.time()
    await _seed(store, "fresh", created_at=now, updated_at=now)  # 0 messages

    q = StoreSessionQuery(store, base_dir=tmp_path)
    rows = await q.list()
    assert rows[0].session_id == "fresh"


async def test_unseen_when_activity_after_view(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(
        store,
        "s",
        extra={"last_activity_at": 100.0, "last_viewed_at": 50.0},
    )
    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()
    assert row.unseen is True
    assert row.status == "completed_unseen"


async def test_seen_when_view_after_activity(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(
        store,
        "s",
        extra={"last_activity_at": 100.0, "last_viewed_at": 150.0},
    )
    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()
    assert row.unseen is False
    assert row.status == "idle"


async def test_error_outcome_yields_error_unseen(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(
        store,
        "s",
        extra={
            "last_activity_at": 100.0,
            "last_viewed_at": 50.0,
            "last_outcome": "error",
        },
    )
    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()
    assert row.status == "error_unseen"


async def test_group_root_surfaces(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "s", cwd="/wt/x", extra={"group_root": "/proj"})
    q = StoreSessionQuery(store, base_dir=tmp_path)
    (row,) = await q.list()
    assert row.group_root == "/proj"
    assert row.cwd == "/wt/x"


async def test_pinned_projection_comes_from_cached_meta(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "s", extra={"pinned": True})
    q = StoreSessionQuery(store, base_dir=tmp_path)

    (row,) = await q.list()

    assert row.pinned is True


async def test_catalog_migrates_legacy_navigation_state_once(tmp_path) -> None:
    store = JSONLStore(tmp_path / "sessions")
    await _seed(
        store,
        "s",
        title="Migrated",
        extra={
            "last_activity_at": 100.0,
            "last_viewed_at": 50.0,
            "pinned": True,
        },
    )
    catalog = SQLiteSessionCatalog(tmp_path / "session-catalog.sqlite3")
    query = StoreSessionQuery(
        store,
        base_dir=tmp_path / "sessions",
        catalog=catalog,
    )

    (row,) = await query.list()

    assert row.title == "Migrated"
    assert row.pinned is True
    assert row.unseen is True


async def test_catalog_owned_pin_survives_projection_repair(tmp_path) -> None:
    session_root = tmp_path / "sessions"
    store = JSONLStore(session_root)
    await _seed(store, "s", extra={"pinned": True})
    catalog = SQLiteSessionCatalog(tmp_path / "session-catalog.sqlite3")
    first = StoreSessionQuery(store, base_dir=session_root, catalog=catalog)
    await first.list()
    await catalog.set_pinned("s", False)

    await store.update_meta("s", title="Changed", pinned=True)
    repaired = StoreSessionQuery(store, base_dir=session_root, catalog=catalog)
    (row,) = await repaired.list()

    assert row.title == "Changed"
    assert row.pinned is False


async def test_rename_writes_title_and_invalidates_cache(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "s", title="Before")
    q = StoreSessionQuery(store, base_dir=tmp_path)
    await q.list()  # warm the cache
    await q.rename("s", "After")
    (row,) = await q.list()
    assert row.title == "After"


async def test_delete_removes_session(tmp_path) -> None:
    store = JSONLStore(tmp_path)
    await _seed(store, "a")
    await _seed(store, "b")
    q = StoreSessionQuery(store, base_dir=tmp_path)
    await q.list()
    await q.delete("a")
    rows = await q.list()
    assert [r.session_id for r in rows] == ["b"]
