"""StoreSessionQuery — the default :class:`SessionListQuery` adapter (§4.2/§9.2).

A thin, read-only wrapper over a :class:`~autoverse.ports.Store` that enumerates
sessions for the TUI / Web session rails. It owns the "where does each display
number come from" logic (§4.2) but **no domain logic** and **no engine privates**
(P3): it reads ``Store.list_sessions``/``load_meta``, counts user prompts for
the displayed prompt count, and probes the cross-process busy lock — never reflecting an
``EngineHandle``'s ``_store``.

Cost discipline (§4.2.1): a per-session ``{sid -> (mtime, count, meta)}`` cache
keyed on the session file's mtime means ``list()`` only re-reads sessions whose
file actually changed; the busy probe ``os.kill(pid, 0)`` runs **only** for
sessions that have a ``.busy`` marker (a cheap ``stat``), not every row.

Status / unseen: computed from persisted timestamps
(``last_activity_at`` vs ``last_viewed_at``) + the cross-process busy lock. The
*current* session's live ``running``/``awaiting_input`` is overlaid by the app
(it alone holds that truth, §5.6.7) — this adapter leaves the current session at
the timestamp-derived state and never invents a live status for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
import json
import os
from pathlib import Path

from autoverse.infra.persist.busy_lock import BusyLock
from autoverse.ports import (
    SessionListQuery,
    SessionMeta,
    SessionCatalogPort,
    SessionProjection,
    SessionStatus,
    SessionSummary,
    Store,
)
from autoverse.types.messages import Message, TextBlock


class StoreSessionQuery(SessionListQuery):
    """Read-only session enumeration backed by a ``Store`` (+ optional file dir)."""

    def __init__(
        self,
        store: Store,
        *,
        base_dir: Path | str | None = None,
        busy: BusyLock | None = None,
        catalog: SessionCatalogPort | None = None,
    ) -> None:
        self._store = store
        # When backed by the JSONLStore we know the on-disk layout, enabling the
        # cheap mtime cache + JSONL prompt-count path. Without it we degrade to
        # loading messages for the count (no cache) — correct, just not as cheap.
        self._base: Path | None = Path(base_dir) if base_dir else None
        self._busy = busy or (BusyLock(self._base) if self._base else None)
        self._catalog = catalog
        self._catalog_ready = False
        self._catalog_lock = asyncio.Lock()
        # sid -> (mtime, prompt_count, SessionMeta) — invalidated by mtime change.
        self._cache: dict[str, tuple[float, int, SessionMeta]] = {}

    # ── SessionListQuery ─────────────────────────────────────────────────────
    async def list(self) -> list[SessionSummary]:
        if self._catalog is not None:
            await self._ensure_catalog()
            catalog_summaries = await self._catalog.list()
            return [
                self._with_busy_status(summary) for summary in catalog_summaries
            ]
        ids = await self._store.list_sessions()
        summaries: list[SessionSummary] = []
        for sid in ids:
            count, meta, mtime = await self._meta_and_count(sid)
            summaries.append(self._summarise(sid, count, meta, mtime))
        # Sorting is the implementation's job (§4.2): newest activity first.
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    async def delete(self, session_id: str) -> None:
        self._cache.pop(session_id, None)
        await self._store.delete(session_id)
        if self._catalog is not None:
            await self._catalog.delete(session_id)

    async def rename(self, session_id: str, title: str) -> None:
        self._cache.pop(session_id, None)
        # Mark the title as user-locked so the per-turn auto-title generator
        # (EngineAdapter._stamp_meta) never overwrites a manual rename.
        await self._store.update_meta(session_id, title=title, title_locked=True)
        if self._catalog is not None:
            await self._catalog.rename(session_id, title)

    async def _ensure_catalog(self) -> None:
        if self._catalog_ready or self._catalog is None:
            return
        async with self._catalog_lock:
            if self._catalog_ready or self._catalog is None:
                return
            versions = await self._catalog.source_versions()
            ids = await self._store.list_sessions()
            live_ids = set(ids)
            for sid in ids:
                mtime_ns = self._source_mtime_ns(sid)
                if versions.get(sid) == mtime_ns and mtime_ns > 0:
                    continue
                count, meta, mtime = await self._meta_and_count(sid)
                await self._catalog.upsert_projection(
                    session_projection(
                        meta,
                        count=count,
                        mtime=mtime,
                        source_mtime_ns=mtime_ns,
                    )
                )
            for stale_id in versions.keys() - live_ids:
                await self._catalog.delete(stale_id)
            self._catalog_ready = True

    def _source_mtime_ns(self, sid: str) -> int:
        if self._base is None:
            return 0
        try:
            return (self._base / f"{sid}.jsonl").stat().st_mtime_ns
        except OSError:
            return 0

    def _with_busy_status(self, summary: SessionSummary) -> SessionSummary:
        if self._is_busy_external(summary.session_id):
            return replace(summary, status="busy_external")
        return summary

    # ── helpers ──────────────────────────────────────────────────────────────
    async def _meta_and_count(self, sid: str) -> tuple[int, SessionMeta, float]:
        """Return (prompt_count, meta, file_mtime), reusing the mtime cache.

        ``file_mtime`` is the session file's last-modified time — used as the
        last-reply fallback for legacy sessions whose meta predates the
        ``updated_at``/``last_activity_at`` stamps (§9.1/§9.4)."""
        path = self._base / f"{sid}.jsonl" if self._base else None
        mtime = 0.0
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            cached = self._cache.get(sid)
            if cached is not None and cached[0] == mtime:
                return cached[1], cached[2], mtime
        meta = await self._store.load_meta(sid)
        count = await self._count_user_prompts(sid, path)
        if path is not None:
            self._cache[sid] = (mtime, count, meta)
        return count, meta, mtime

    async def _count_user_prompts(self, sid: str, path: "Path | None") -> int:
        """Display count = number of user prompts, not store messages.

        Tool results are stored as role=user messages too, so the old
        ``file lines - 1`` count inflated the rail number on tool-heavy turns.
        Count only user-role messages that contain a text block: those are the
        prompts the user (or a prompt-template command) submitted. The JSONL path
        stays cheap by scanning each line's role/content shape without rebuilding
        full Message objects; non-file stores fall back to Store.load()."""
        if path is not None and path.exists():
            try:
                n = 0
                with path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if _is_user_prompt_dict(obj):
                            n += 1
                return n
            except OSError:
                pass
        try:
            msgs = await self._store.load(sid)
            return sum(
                1
                for m in msgs
                if m.role == "user"
                and any(isinstance(b, TextBlock) for b in m.content)
            )
        except Exception:  # noqa: BLE001 — listing must never crash on one bad file
            return 0

    def _summarise(
        self, sid: str, count: int, meta: SessionMeta, mtime: float = 0.0
    ) -> SessionSummary:
        extra = meta.extra if isinstance(meta.extra, dict) else {}
        last_activity = _as_float(extra.get("last_activity_at"))
        last_viewed = _as_float(extra.get("last_viewed_at"))
        last_outcome = str(extra.get("last_outcome", "") or "")
        # "Last reply" sort key = the latest of every signal we trust:
        #   • last_activity_at — precise, stamped at turn end (§9.4).
        #   • meta.updated_at / created_at — a fresh session stamps both = now
        #     (builder), so a 0-message new session sorts to its group top.
        #   • file mtime — LEGACY fallback only (pre-stamp session with no
        #     last_activity_at): the file's final append ≈ its last reply.
        #     Crucially NOT used once last_activity_at exists, because every meta
        #     rewrite bumps mtime — including the view-only last_viewed_at stamp
        #     on switch-in (_mark_viewed). Trusting mtime there floated the
        #     *current* session to its group top on every switch (position churn,
        #     the bug the user reported). New sessions still top via created_at;
        #     real sessions order by last_activity_at, immune to view churn.
        updated_at = max(last_activity, meta.updated_at, meta.created_at)
        if count > 0 and last_activity <= 0.0:
            updated_at = max(updated_at, mtime)
        updated_at = updated_at or meta.created_at
        unseen = last_activity > 0.0 and last_activity > last_viewed
        status = self._status(sid, unseen, last_outcome)
        return SessionSummary(
            session_id=sid,
            title=meta.title,
            model=meta.model,
            cwd=meta.cwd,
            created_at=meta.created_at,
            updated_at=updated_at,
            message_count=count,
            status=status,
            unseen=unseen,
            pinned=bool(extra.get("pinned")),
            group_root=str(extra.get("group_root", "") or ""),
        )

    def _status(self, sid: str, unseen: bool, last_outcome: str) -> SessionStatus:
        """Phase-1 status: cross-process busy + timestamp-derived unseen.

        ``running``/``awaiting_input`` for the *current* session are overlaid by
        the app (§5.6.7); this stays at the persisted truth, which is also what a
        cold / cross-process session shows."""
        if self._is_busy_external(sid):
            return "busy_external"
        if unseen:
            return "error_unseen" if last_outcome == "error" else "completed_unseen"
        return "idle"

    def _is_busy_external(self, sid: str) -> bool:
        """True only if a *different live* process holds the busy lock (§5.7.1)."""
        if self._busy is None:
            return False
        holder = self._busy.holder(sid)
        if holder is None:
            return False
        try:
            raw_pid = holder.get("pid")
            pid = int(raw_pid) if isinstance(raw_pid, (int, float, str)) else 0
        except (ValueError, TypeError):
            return False
        if pid == os.getpid():
            return False  # our own lock (current running session) — app overlays it
        # is_busy self-heals stale locks (dead pid) via os.kill(pid, 0) (§8).
        return self._busy.is_busy(sid)


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def session_projection(
    meta: SessionMeta,
    *,
    count: int,
    mtime: float = 0.0,
    source_mtime_ns: int = 0,
) -> SessionProjection:
    """Build the catalog's rebuildable projection from Store-owned state."""
    extra = meta.extra if isinstance(meta.extra, dict) else {}
    last_activity = _as_float(extra.get("last_activity_at"))
    updated_at = max(last_activity, meta.updated_at, meta.created_at)
    if count > 0 and last_activity <= 0.0:
        updated_at = max(updated_at, mtime)
    return SessionProjection(
        session_id=meta.session_id,
        title=meta.title,
        model=meta.model,
        cwd=meta.cwd,
        group_root=str(extra.get("group_root", "") or ""),
        created_at=meta.created_at,
        updated_at=updated_at or meta.created_at,
        message_count=count,
        last_activity_at=last_activity,
        last_outcome=str(extra.get("last_outcome", "") or ""),
        source_mtime_ns=source_mtime_ns,
        legacy_last_viewed_at=_as_float(extra.get("last_viewed_at")),
        legacy_pinned=bool(extra.get("pinned")),
    )


async def sync_session_catalog(
    catalog: SessionCatalogPort,
    store: Store,
    session_id: str,
    messages: Sequence[Message],
    *,
    source_path: Path | None = None,
) -> None:
    """Refresh one catalog projection after Store state has committed."""
    meta = await store.load_meta(session_id)
    count = sum(
        1
        for message in messages
        if message.role == "user"
        and any(isinstance(block, TextBlock) for block in message.content)
    )
    mtime = 0.0
    mtime_ns = 0
    if source_path is not None:
        try:
            stat = source_path.stat()
            mtime = stat.st_mtime
            mtime_ns = stat.st_mtime_ns
        except OSError:
            pass
    await catalog.upsert_projection(
        session_projection(
            meta,
            count=count,
            mtime=mtime,
            source_mtime_ns=mtime_ns,
        )
    )


def _is_user_prompt_dict(obj: object) -> bool:
    """True for persisted user prompt messages, false for tool-result messages."""
    if not isinstance(obj, dict):
        return False
    if obj.get("_kind") != "msg" or obj.get("role") != "user":
        return False
    content = obj.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in content)


__all__ = [
    "StoreSessionQuery",
    "session_projection",
    "sync_session_catalog",
]
