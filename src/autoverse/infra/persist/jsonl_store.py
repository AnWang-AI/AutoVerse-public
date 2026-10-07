"""JSONLStore — the default ``Store`` adapter (docs/reference/events-and-storage.md §4/§5/§9/§10).

File layout (``<base_dir>/<session_id>.jsonl``):

    line 0 : {"_kind": "meta", ...}   — SessionMeta
    line 1+: {"_kind": "msg",  ...}   — Message

Write discipline (single write path, P1):

- ``append`` builds the full byte string for the message(s) and writes them in
  one ``O_APPEND`` ``write`` so concurrent appends never interleave (D8).
- ``rewrite`` writes a temp file then ``os.replace`` (atomic) and records the
  ``reason`` in the meta header (D3); it holds a cross-process ``flock`` so it
  is mutually exclusive with other rewrites (D8).
- ``load`` is corruption-tolerant: malformed lines are skipped and counted in a
  :class:`LoadReport` rather than failing the whole load (D4); the report is
  exposed via :meth:`load_with_report`.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import mmap
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from autoverse.infra.persist.serialize import (
    BlobStore,
    FileBlobStore,
    msg_from_dict,
    msg_to_dict,
)
from autoverse.ports import SessionHistoryWindow, SessionMeta
from autoverse.types.messages import Message


@dataclass
class LoadReport:
    """Outcome of a corruption-tolerant load (docs/reference/events-and-storage.md §10.1)."""

    loaded: int = 0
    skipped: int = 0
    repaired: int = 0
    skipped_lines: list[int] = field(default_factory=list)


class JSONLStore:
    """Append-only JSONL session store implementing the ``Store`` Protocol."""

    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._blobs: BlobStore = FileBlobStore(self._base)
        self._locks: dict[str, asyncio.Lock] = {}

    # ── paths / helpers ──────────────────────────────────────────────────────

    def _path(self, sid: str) -> Path:
        return self._base / f"{sid}.jsonl"

    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    def _encode_msg_line(self, m: Message) -> bytes:
        return (
            json.dumps(msg_to_dict(m, blobs=self._blobs), ensure_ascii=False) + "\n"
        ).encode("utf-8")

    def _encode_meta_line(self, meta: SessionMeta) -> bytes:
        return (json.dumps(_meta_to_dict(meta), ensure_ascii=False) + "\n").encode("utf-8")

    # ── Store Protocol ───────────────────────────────────────────────────────

    async def create(self, session_id: str, meta: SessionMeta) -> None:
        path = self._path(session_id)
        async with self._lock(session_id):
            if path.exists():
                return  # idempotent: never clobber existing history
            with path.open("ab") as f:
                f.write(self._encode_meta_line(meta))

    async def append(self, session_id: str, *msgs: Message) -> None:
        if not msgs:
            return
        path = self._path(session_id)
        data = b"".join(self._encode_msg_line(m) for m in msgs)
        async with self._lock(session_id):
            if not path.exists():
                await self._ensure_meta(session_id)
            # Single O_APPEND write so concurrent appends never interleave (D8),
            # held under a *shared* cross-process flock so it is mutually
            # exclusive with rewrite's exclusive lock: otherwise a rewriter's
            # os.replace could swap the inode between our open() and write(),
            # landing the append in the orphaned old inode and losing it
            # (docs/reference/events-and-storage.md §9.2). Multiple appenders still proceed in
            # parallel (shared lock), keeping the append path lock-light.
            lock_path = self._base / f"{session_id}.lock"
            with lock_path.open("w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                    try:
                        os.write(fd, data)
                    finally:
                        os.close(fd)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    async def load(self, session_id: str, *, since: int = 0) -> list[Message]:
        msgs, _report = self._load_inner(session_id, since=since)
        return msgs

    async def load_with_report(
        self, session_id: str, *, since: int = 0
    ) -> tuple[list[Message], LoadReport]:
        """Corruption-tolerant load that also returns a :class:`LoadReport`."""
        return self._load_inner(session_id, since=since)

    async def load_tail(
        self, session_id: str, *, minimum_turns: int = 2
    ) -> SessionHistoryWindow:
        """Read newest complete turns by scanning the JSONL file backwards."""
        return await asyncio.to_thread(
            self._load_window_sync,
            session_id,
            None,
            max(1, minimum_turns),
        )

    async def load_before(
        self, session_id: str, before_cursor: str
    ) -> SessionHistoryWindow:
        """Read one complete turn before a cursor returned by this adapter."""
        if not before_cursor.startswith("jsonl:"):
            return SessionHistoryWindow(())
        try:
            inode_raw, end_raw = before_cursor.removeprefix("jsonl:").split(":", 1)
            inode = int(inode_raw)
            end = int(end_raw)
        except ValueError:
            return SessionHistoryWindow(())
        path = self._path(session_id)
        try:
            if path.stat().st_ino != inode:
                return SessionHistoryWindow(())
        except OSError:
            return SessionHistoryWindow(())
        return await asyncio.to_thread(self._load_window_sync, session_id, end, 1)

    async def rewrite(self, session_id: str, msgs: list[Message], reason: str) -> None:
        path = self._path(session_id)
        meta = await self._load_meta_safe(session_id)
        extra = dict(meta.extra)
        extra["last_rewrite_reason"] = reason
        meta.extra = extra
        lines = [self._encode_meta_line(meta)]
        lines.extend(self._encode_msg_line(m) for m in msgs)
        body = b"".join(lines)
        async with self._lock(session_id):
            lock_path = self._base / f"{session_id}.lock"
            with lock_path.open("w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    tmp = self._base / f".{session_id}.{uuid.uuid4().hex}.tmp"
                    tmp.write_bytes(body)
                    os.replace(tmp, path)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    async def load_meta(self, session_id: str) -> SessionMeta:
        return await self._load_meta_safe(session_id)

    async def update_meta(self, session_id: str, **fields: object) -> None:
        """Atomically update meta fields under the exclusive cross-process lock.

        The read (meta + messages) and the rewrite happen **inside the same
        ``flock(LOCK_EX)``** so a concurrent ``append`` from another process
        cannot land a message between our read and our rewrite — which would
        otherwise be silently lost when the rewrite's ``os.replace`` swaps the
        inode (cross-process data-loss fix)."""
        async with self._lock(session_id):
            lock_path = self._base / f"{session_id}.lock"
            with lock_path.open("w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    meta = await self._load_meta_safe(session_id)
                    for k, v in fields.items():
                        if hasattr(meta, k) and k != "extra":
                            setattr(meta, k, v)
                        else:
                            meta.extra[k] = v
                    msgs, _report = self._load_inner(session_id, since=0)
                    lines = [self._encode_meta_line(meta)]
                    lines.extend(self._encode_msg_line(m) for m in msgs)
                    body = b"".join(lines)
                    tmp = self._base / f".{session_id}.{uuid.uuid4().hex}.tmp"
                    tmp.write_bytes(body)
                    os.replace(tmp, self._path(session_id))
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    async def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self._base.glob("*.jsonl"))

    async def delete(self, session_id: str) -> None:
        async with self._lock(session_id):
            self._path(session_id).unlink(missing_ok=True)
            (self._base / f"{session_id}.lock").unlink(missing_ok=True)

    # ── internals ────────────────────────────────────────────────────────────

    def _load_inner(self, session_id: str, *, since: int) -> tuple[list[Message], LoadReport]:
        path = self._path(session_id)
        report = LoadReport()
        if not path.exists():
            return [], report
        msgs: list[Message] = []
        msg_index = 0
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f):
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    report.skipped += 1
                    report.skipped_lines.append(lineno)
                    continue
                if not isinstance(obj, dict) or obj.get("_kind") == "meta":
                    continue
                if obj.get("_kind") != "msg":
                    report.skipped += 1
                    report.skipped_lines.append(lineno)
                    continue
                try:
                    msg = msg_from_dict(obj, blobs=self._blobs)
                except (KeyError, TypeError, ValueError):
                    report.skipped += 1
                    report.skipped_lines.append(lineno)
                    continue
                if msg_index >= since:
                    msgs.append(msg)
                msg_index += 1
        report.loaded = len(msgs)
        return msgs, report

    def _load_window_sync(
        self, session_id: str, end: int | None, minimum_turns: int
    ) -> SessionHistoryWindow:
        path = self._path(session_id)
        if not path.exists() or path.stat().st_size == 0:
            return SessionHistoryWindow(())
        newest_groups: list[list[Message]] = []
        current_reversed: list[Message] = []
        oldest_start = 0
        has_older = False
        with path.open("rb") as file:
            with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                position = min(len(mapped), end if end is not None else len(mapped))
                while position > 0:
                    line_end = position
                    if mapped[line_end - 1 : line_end] == b"\n":
                        line_end -= 1
                    line_start = mapped.rfind(b"\n", 0, line_end) + 1
                    position = line_start
                    raw = mapped[line_start:line_end]
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(obj, dict) or obj.get("_kind") != "msg":
                        continue
                    try:
                        message = msg_from_dict(obj, blobs=self._blobs)
                    except (KeyError, TypeError, ValueError):
                        continue
                    current_reversed.append(message)
                    if _is_user_prompt_record(obj):
                        newest_groups.append(list(reversed(current_reversed)))
                        current_reversed = []
                        oldest_start = line_start
                        if len(newest_groups) >= minimum_turns:
                            has_older = _has_message_before(mapped, oldest_start)
                            break
                if position == 0 and current_reversed:
                    newest_groups.append(list(reversed(current_reversed)))
                    oldest_start = 0
        messages = tuple(
            message
            for group in reversed(newest_groups)
            for message in group
        )
        inode = path.stat().st_ino
        before = f"jsonl:{inode}:{oldest_start}" if has_older else None
        return SessionHistoryWindow(messages=messages, before_cursor=before)

    async def _ensure_meta(self, session_id: str) -> None:
        path = self._path(session_id)
        if not path.exists():
            meta = SessionMeta(session_id=session_id)
            with path.open("ab") as f:
                f.write(self._encode_meta_line(meta))

    async def _load_meta_safe(self, session_id: str) -> SessionMeta:
        path = self._path(session_id)
        if not path.exists():
            return SessionMeta(session_id=session_id)
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    break
                if isinstance(obj, dict) and obj.get("_kind") == "meta":
                    return _meta_from_dict(obj, session_id)
                break
        return SessionMeta(session_id=session_id)

    async def _rewrite_with_meta(
        self, session_id: str, meta: SessionMeta, msgs: list[Message]
    ) -> None:
        path = self._path(session_id)
        lines = [self._encode_meta_line(meta)]
        lines.extend(self._encode_msg_line(m) for m in msgs)
        body = b"".join(lines)
        async with self._lock(session_id):
            lock_path = self._base / f"{session_id}.lock"
            with lock_path.open("w") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    tmp = self._base / f".{session_id}.{uuid.uuid4().hex}.tmp"
                    tmp.write_bytes(body)
                    os.replace(tmp, path)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _meta_to_dict(meta: SessionMeta) -> dict[str, object]:
    return {
        "_kind": "meta",
        "session_id": meta.session_id,
        "model": meta.model,
        "cwd": meta.cwd,
        "title": meta.title,
        "created_at": meta.created_at,
        "updated_at": meta.updated_at,
        "extra": dict(meta.extra),
    }


def _meta_from_dict(d: dict[str, object], session_id: str) -> SessionMeta:
    extra = d.get("extra", {})
    return SessionMeta(
        session_id=str(d.get("session_id", session_id)),
        model=str(d.get("model", "")),
        cwd=str(d.get("cwd", "")),
        title=str(d.get("title", "")),
        created_at=float(d.get("created_at", 0.0)),  # type: ignore[arg-type]
        updated_at=float(d.get("updated_at", 0.0)),  # type: ignore[arg-type]
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def _is_user_prompt_record(obj: dict[str, object]) -> bool:
    if obj.get("role") != "user":
        return False
    content = obj.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text"
        for block in content
    )


def _has_message_before(mapped: mmap.mmap, end: int) -> bool:
    position = end
    while position > 0:
        line_end = position
        if mapped[line_end - 1 : line_end] == b"\n":
            line_end -= 1
        line_start = mapped.rfind(b"\n", 0, line_end) + 1
        position = line_start
        raw = mapped[line_start:line_end]
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("_kind") == "msg":
            return True
    return False


__all__ = ["JSONLStore", "LoadReport"]
