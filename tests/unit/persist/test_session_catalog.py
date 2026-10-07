"""Unit tests for the SQLite WAL session-navigation catalog."""

from __future__ import annotations

import sqlite3

from autoverse.infra.persist.session_catalog import SQLiteSessionCatalog
from autoverse.ports import SessionProjection


async def test_projection_upsert_preserves_catalog_owned_fields(tmp_path) -> None:
    catalog = SQLiteSessionCatalog(tmp_path / "catalog.sqlite3")
    projection = SessionProjection(
        session_id="s1",
        title="First",
        updated_at=10.0,
        last_activity_at=10.0,
        legacy_last_viewed_at=5.0,
        legacy_pinned=True,
        source_mtime_ns=1,
    )
    await catalog.upsert_projection(projection)
    await catalog.mark_viewed("s1", 20.0)
    await catalog.set_pinned("s1", False)

    await catalog.upsert_projection(
        SessionProjection(
            session_id="s1",
            title="Updated",
            updated_at=30.0,
            last_activity_at=30.0,
            legacy_last_viewed_at=0.0,
            legacy_pinned=True,
            source_mtime_ns=2,
        )
    )

    row = await catalog.get("s1")
    assert row is not None
    assert row.title == "Updated"
    assert row.pinned is False
    assert row.unseen is True
    assert await catalog.source_versions() == {"s1": 2}


async def test_two_catalog_instances_share_wal_updates(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    first = SQLiteSessionCatalog(path)
    second = SQLiteSessionCatalog(path)
    await first.upsert_projection(
        SessionProjection(session_id="s1", title="Shared", updated_at=1.0)
    )

    await second.set_pinned("s1", True)
    await first.mark_viewed("s1", 12.0)

    row = await second.get("s1")
    assert row is not None
    assert row.pinned is True
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_corrupt_catalog_is_quarantined_and_recreated(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite3"
    path.write_bytes(b"not sqlite")

    catalog = SQLiteSessionCatalog(path)
    await catalog.upsert_projection(SessionProjection(session_id="recovered"))

    assert await catalog.get("recovered") is not None
    assert list(tmp_path.glob("catalog.sqlite3.corrupt-*"))


async def test_catalog_filters_by_indexed_cwd_and_limit(tmp_path) -> None:
    catalog = SQLiteSessionCatalog(tmp_path / "catalog.sqlite3")
    for index in range(200):
        await catalog.upsert_projection(
            SessionProjection(
                session_id=f"s{index:03d}",
                cwd="/workspace/a" if index % 2 == 0 else "/workspace/b",
                updated_at=float(index),
            )
        )

    rows = await catalog.list(cwd="/workspace/a", limit=5)

    assert [row.session_id for row in rows] == [
        "s198", "s196", "s194", "s192", "s190"
    ]
