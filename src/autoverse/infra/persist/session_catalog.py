"""SQLite WAL session-navigation catalog adapter.

See ``docs/design/frontend-interaction-responsiveness.md``.  Projection fields
can be repaired from Store data; the catalog's pinned/viewed fields are kept
across projection upserts and never rewrite transcript JSONL files.
"""

from __future__ import annotations

import asyncio
import builtins
import os
from pathlib import Path
import sqlite3
import time

from autoverse.ports.query import SessionStatus, SessionSummary
from autoverse.ports.session_catalog import SessionCatalogPort, SessionProjection


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_catalog (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    group_root TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_activity_at REAL NOT NULL DEFAULT 0,
    last_viewed_at REAL NOT NULL DEFAULT 0,
    last_outcome TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    source_mtime_ns INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_session_catalog_updated
    ON session_catalog(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_catalog_cwd_updated
    ON session_catalog(cwd, updated_at DESC);
"""


class SQLiteSessionCatalog(SessionCatalogPort):
    """Cross-process navigation catalog using one short SQLite transaction/call."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except sqlite3.DatabaseError:
            self._quarantine_corrupt_files()
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(_SCHEMA)

    def _quarantine_corrupt_files(self) -> None:
        stamp = f"{int(time.time() * 1000)}"
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{self.path}{suffix}")
            if not source.exists():
                continue
            target = source.with_name(f"{source.name}.corrupt-{stamp}")
            os.replace(source, target)

    async def list(
        self, *, cwd: str | None = None, limit: int | None = None
    ) -> builtins.list[SessionSummary]:
        return await asyncio.to_thread(self._list_sync, cwd, limit)

    def _list_sync(
        self, cwd: str | None, limit: int | None
    ) -> builtins.list[SessionSummary]:
        bounded_limit = max(0, int(limit)) if limit is not None else None
        if bounded_limit == 0:
            return []
        sql = "SELECT * FROM session_catalog"
        params: builtins.list[object] = []
        if cwd is not None:
            sql += " WHERE cwd = ?"
            params.append(cwd)
        sql += " ORDER BY updated_at DESC, session_id"
        if bounded_limit is not None:
            sql += " LIMIT ?"
            params.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._summary(row) for row in rows]

    async def get(self, session_id: str) -> SessionSummary | None:
        return await asyncio.to_thread(self._get_sync, session_id)

    def _get_sync(self, session_id: str) -> SessionSummary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_catalog WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._summary(row) if row is not None else None

    async def source_versions(self) -> dict[str, int]:
        return await asyncio.to_thread(self._source_versions_sync)

    def _source_versions_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, source_mtime_ns FROM session_catalog"
            ).fetchall()
        return {
            str(row["session_id"]): int(row["source_mtime_ns"] or 0)
            for row in rows
        }

    @staticmethod
    def _summary(row: sqlite3.Row) -> SessionSummary:
        last_activity = float(row["last_activity_at"] or 0.0)
        last_viewed = float(row["last_viewed_at"] or 0.0)
        unseen = last_activity > 0.0 and last_activity > last_viewed
        status: SessionStatus = "idle"
        if unseen:
            status = (
                "error_unseen"
                if str(row["last_outcome"] or "") == "error"
                else "completed_unseen"
            )
        return SessionSummary(
            session_id=str(row["session_id"]),
            title=str(row["title"] or ""),
            model=str(row["model"] or ""),
            cwd=str(row["cwd"] or ""),
            group_root=str(row["group_root"] or ""),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            message_count=int(row["message_count"] or 0),
            status=status,
            unseen=unseen,
            pinned=bool(row["pinned"]),
        )

    async def upsert_projection(self, projection: SessionProjection) -> None:
        await asyncio.to_thread(self._upsert_projection_sync, projection)

    def _upsert_projection_sync(self, projection: SessionProjection) -> None:
        values = (
            projection.session_id,
            projection.title,
            projection.model,
            projection.cwd,
            projection.group_root,
            projection.created_at,
            projection.updated_at,
            projection.message_count,
            projection.last_activity_at,
            projection.legacy_last_viewed_at,
            projection.last_outcome,
            int(projection.legacy_pinned),
            projection.source_mtime_ns,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_catalog (
                    session_id, title, model, cwd, group_root, created_at,
                    updated_at, message_count, last_activity_at, last_viewed_at,
                    last_outcome, pinned, source_mtime_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    model = excluded.model,
                    cwd = excluded.cwd,
                    group_root = excluded.group_root,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    message_count = excluded.message_count,
                    last_activity_at = excluded.last_activity_at,
                    last_outcome = excluded.last_outcome,
                    source_mtime_ns = excluded.source_mtime_ns
                """,
                values,
            )

    async def mark_viewed(self, session_id: str, viewed_at: float) -> None:
        await asyncio.to_thread(self._mark_viewed_sync, session_id, viewed_at)

    def _mark_viewed_sync(self, session_id: str, viewed_at: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_catalog (session_id, last_viewed_at)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    last_viewed_at = MAX(last_viewed_at, excluded.last_viewed_at)
                """,
                (session_id, viewed_at),
            )

    async def set_pinned(self, session_id: str, pinned: bool) -> None:
        await asyncio.to_thread(self._set_pinned_sync, session_id, pinned)

    def _set_pinned_sync(self, session_id: str, pinned: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_catalog (session_id, pinned)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET pinned = excluded.pinned
                """,
                (session_id, int(pinned)),
            )

    async def rename(self, session_id: str, title: str) -> None:
        await asyncio.to_thread(self._rename_sync, session_id, title)

    def _rename_sync(self, session_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_catalog (session_id, title)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET title = excluded.title
                """,
                (session_id, title),
            )

    async def delete(self, session_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, session_id)

    def _delete_sync(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM session_catalog WHERE session_id = ?", (session_id,)
            )


__all__ = ["SQLiteSessionCatalog"]
