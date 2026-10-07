"""Durable and in-memory adapters for compacted conversation context.

The archive is a recovery surface, not an observability trace.  Removed
messages are redacted before persistence, large payloads use the existing
content-addressed blob codec, and reads are always session-scoped and bounded.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path

from autoverse.infra.observ.redact import Redactor
from autoverse.infra.persist.serialize import FileBlobStore, msg_from_dict, msg_to_dict
from autoverse.types import Message, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock
from autoverse.types.context import (
    CompactionArchiveDiagnostics,
    CompactionCatalogEntry,
    CompactionSearchHit,
    CompactionSegmentRef,
    CompactionSlice,
)

_SCHEMA_VERSION = 1
_PATH_RE = re.compile(r"(?<![\w.-])(?:/?[\w.@+-]+/)+[\w.@+-]+")
_ERROR_RE = re.compile(
    r"(?im)^.*(?:traceback|failed|error|exception|assertionerror).{0,240}$"
)


def _validate_session_id(session_id: str) -> str:
    if (
        not session_id
        or session_id in {".", ".."}
        or Path(session_id).name != session_id
        or "/" in session_id
        or "\\" in session_id
    ):
        raise ValueError("session_id must be a single safe path component")
    return session_id


def _message_text(messages: tuple[Message, ...]) -> str:
    lines: list[str] = []
    for message in messages:
        lines.append(f"[{message.role}]")
        for block in message.content:
            if isinstance(block, (TextBlock, ThinkingBlock)):
                lines.append(block.text)
            elif isinstance(block, ToolUseBlock):
                lines.append(
                    f"tool_use {block.name} {json.dumps(block.input, ensure_ascii=False, sort_keys=True)}"
                )
            elif isinstance(block, ToolResultBlock):
                lines.append(f"tool_result {block.tool_use_id}\n{block.content}")
            else:
                lines.append("[image]")
    return "\n".join(lines)


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _catalog(
    ref: CompactionSegmentRef, messages: tuple[Message, ...]
) -> CompactionCatalogEntry:
    text = _message_text(messages)
    tools: set[str] = set()
    commands: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                tools.add(block.name)
                command = block.input.get("command")
                if isinstance(command, str) and command.strip():
                    commands.add(command.strip()[:500])
    paths = tuple(dict.fromkeys(_PATH_RE.findall(text)))[:50]
    errors = tuple(
        dict.fromkeys(match.group(0).strip() for match in _ERROR_RE.finditer(text))
    )[:20]
    preview = " ".join(text.split())[:500]
    return CompactionCatalogEntry(
        ref=ref,
        tool_names=tuple(sorted(tools)),
        paths=paths,
        commands=tuple(sorted(commands)),
        error_signatures=errors,
        preview=preview,
    )


def _ref_to_dict(ref: CompactionSegmentRef) -> dict[str, object]:
    return {
        "session_id": ref.session_id,
        "segment_id": ref.segment_id,
        "kind": ref.kind,
        "created_at": ref.created_at,
        "turn_start": ref.turn_start,
        "turn_end": ref.turn_end,
        "message_count": ref.message_count,
        "content_sha256": ref.content_sha256,
    }


def _ref_from_dict(data: dict[str, object]) -> CompactionSegmentRef:
    def _int(value: object, default: int = 0) -> int:
        return int(value) if isinstance(value, (str, int, float)) else default

    def _float(value: object) -> float:
        return float(value) if isinstance(value, (str, int, float)) else 0.0

    return CompactionSegmentRef(
        session_id=str(data["session_id"]),
        segment_id=str(data["segment_id"]),
        kind=str(data.get("kind", "")),
        created_at=_float(data.get("created_at", 0.0)),
        turn_start=_int(data.get("turn_start", 0)),
        turn_end=_int(data.get("turn_end", 0)),
        message_count=_int(data.get("message_count", 0)),
        content_sha256=str(data.get("content_sha256", "")),
    )


class FileCompactionArchive:
    """Append-only local archive with corruption-tolerant bounded reads."""

    def __init__(self, base_dir: Path | str, *, redactor: Redactor | None = None) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._redactor = redactor or Redactor()
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconciled: dict[
            str, tuple[int, int, CompactionArchiveDiagnostics]
        ] = {}

    def _dir(self, session_id: str) -> Path:
        return self._base / _validate_session_id(session_id)

    def _path(self, session_id: str) -> Path:
        return self._dir(session_id) / "segments.jsonl"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _index_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "index.sqlite3"

    def _open_index(self, session_id: str) -> sqlite3.Connection:
        try:
            return self._open_index_once(session_id)
        except sqlite3.DatabaseError:
            # The index is derived state. Remove corrupt SQLite artifacts and
            # recreate; authoritative JSONL/blob data is reconciled by callers.
            base = self._index_path(session_id)
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(f"{base}{suffix}").unlink(missing_ok=True)
            self._reconciled.pop(session_id, None)
            return self._open_index_once(session_id)

    def _open_index_once(self, session_id: str) -> sqlite3.Connection:
        directory = self._dir(session_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._index_path(session_id)
        connection = sqlite3.connect(path)
        path.chmod(0o600)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS segments (
                rowid INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                created_at REAL NOT NULL,
                turn_start INTEGER NOT NULL,
                turn_end INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                search_text TEXT NOT NULL,
                search_normalized TEXT NOT NULL,
                tool_names TEXT NOT NULL,
                paths TEXT NOT NULL,
                commands TEXT NOT NULL,
                error_signatures TEXT NOT NULL,
                preview TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
                search_text,
                content='segments',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        return connection

    @staticmethod
    def _insert_index_record(
        connection: sqlite3.Connection,
        ref: CompactionSegmentRef,
        messages: tuple[Message, ...],
    ) -> None:
        text = _message_text(messages)
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual_hash != ref.content_sha256:
            raise ValueError(f"archive segment integrity check failed: {ref.segment_id}")
        catalog = _catalog(ref, messages)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO segments (
                segment_id, kind, created_at, turn_start, turn_end,
                message_count, content_sha256, search_text, search_normalized,
                tool_names, paths, commands, error_signatures, preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ref.segment_id,
                ref.kind,
                ref.created_at,
                ref.turn_start,
                ref.turn_end,
                ref.message_count,
                ref.content_sha256,
                text,
                _normalise(text),
                json.dumps(catalog.tool_names, ensure_ascii=False),
                json.dumps(catalog.paths, ensure_ascii=False),
                json.dumps(catalog.commands, ensure_ascii=False),
                json.dumps(catalog.error_signatures, ensure_ascii=False),
                catalog.preview,
            ),
        )
        if cursor.rowcount:
            rowid = connection.execute(
                "SELECT rowid FROM segments WHERE segment_id = ?", (ref.segment_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO segments_fts(rowid, search_text) VALUES (?, ?)",
                (rowid, text),
            )

    def _index_messages_sync(
        self, ref: CompactionSegmentRef, messages: tuple[Message, ...]
    ) -> None:
        with self._open_index(ref.session_id) as connection:
            self._insert_index_record(connection, ref, messages)

    def _reconcile_index_sync(self, session_id: str) -> CompactionArchiveDiagnostics:
        path = self._path(session_id)
        if not path.exists():
            return CompactionArchiveDiagnostics()
        stat = path.stat()
        cached = self._reconciled.get(session_id)
        if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            with self._open_index(session_id) as connection:
                count = int(
                    connection.execute("SELECT count(*) FROM segments").fetchone()[0]
                )
            if count == cached[2].indexed_segments:
                return cached[2]
        blobs = FileBlobStore(self._dir(session_id))
        corrupt = 0
        with self._open_index(session_id) as connection:
            indexed = {
                str(row[0])
                for row in connection.execute("SELECT segment_id FROM segments")
            }
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        data = json.loads(raw)
                        if data.get("_kind") != "compaction_segment":
                            continue
                        ref_data = data["ref"]
                        msg_data = data["messages"]
                        if not isinstance(ref_data, dict) or not isinstance(msg_data, list):
                            raise ValueError("invalid archive record shape")
                        ref = _ref_from_dict(ref_data)
                        if ref.session_id != session_id:
                            raise ValueError("archive session mismatch")
                        if ref.segment_id in indexed:
                            continue
                        messages = tuple(
                            msg_from_dict(item, blobs=blobs)
                            for item in msg_data
                            if isinstance(item, dict)
                        )
                        self._insert_index_record(connection, ref, messages)
                        indexed.add(ref.segment_id)
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        corrupt += 1
            connection.execute(
                "INSERT OR REPLACE INTO archive_meta(key, value) VALUES ('corrupt_records', ?)",
                (corrupt,),
            )
            diagnostics = CompactionArchiveDiagnostics(
                corrupt_records=corrupt, indexed_segments=len(indexed)
            )
            self._reconciled[session_id] = (
                stat.st_mtime_ns,
                stat.st_size,
                diagnostics,
            )
            return diagnostics

    async def append(
        self,
        session_id: str,
        *,
        kind: str,
        messages: tuple[Message, ...],
        turn_start: int,
        turn_end: int,
    ) -> CompactionSegmentRef:
        _validate_session_id(session_id)
        if not messages:
            raise ValueError("cannot archive an empty segment")
        redacted = tuple(self._redactor.message(message) for message in messages)
        text = _message_text(redacted)
        ref = CompactionSegmentRef(
            session_id=session_id,
            segment_id=f"seg-{uuid.uuid4().hex}",
            kind=kind,
            created_at=time.time(),
            turn_start=max(0, turn_start),
            turn_end=max(turn_start, turn_end),
            message_count=len(redacted),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        async with self._lock(session_id):
            await asyncio.to_thread(self._append_sync, ref, redacted)
        return ref

    def _append_sync(
        self, ref: CompactionSegmentRef, messages: tuple[Message, ...]
    ) -> None:
        directory = self._dir(ref.session_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        blobs = FileBlobStore(directory)
        record = {
            "_kind": "compaction_segment",
            "schema_version": _SCHEMA_VERSION,
            "ref": _ref_to_dict(ref),
            "messages": [msg_to_dict(message, blobs=blobs) for message in messages],
        }
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        lock_path = directory / "segments.lock"
        with lock_path.open("a+b") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                fd = os.open(
                    self._path(ref.session_id),
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        try:
            self._index_messages_sync(ref, messages)
        except sqlite3.Error:
            # The JSONL/blob archive is authoritative. A missing derived index is
            # rebuilt on the next browse/search operation.
            pass

    async def read(
        self,
        session_id: str,
        segment_id: str,
        *,
        offset: int = 0,
        max_chars: int = 8_000,
    ) -> CompactionSlice:
        if offset < 0 or max_chars <= 0:
            raise ValueError("offset must be >= 0 and max_chars must be > 0")
        found = await asyncio.to_thread(
            self._find_record_sync, session_id, segment_id
        )
        if found is None:
            raise KeyError(f"compaction segment not found: {segment_id}")
        ref, messages = found
        text = _message_text(messages)
        actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual_hash != ref.content_sha256:
            raise ValueError(f"archive segment integrity check failed: {segment_id}")
        page = text[offset : offset + max_chars]
        next_offset = offset + len(page) if offset + len(page) < len(text) else None
        return CompactionSlice(
            ref=ref,
            text=page,
            offset=offset,
            next_offset=next_offset,
            truncated=next_offset is not None,
        )

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        max_results: int = 5,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]:
        needle = _normalise(query)
        if not needle:
            raise ValueError("query must not be empty")
        return await asyncio.to_thread(
            self._search_index_sync,
            session_id,
            query,
            max(1, max_results),
            max(64, excerpt_chars),
        )

    async def grep(
        self,
        session_id: str,
        pattern: str,
        *,
        regex: bool = False,
        segment_id: str | None = None,
        max_results: int = 20,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]:
        if not pattern.strip():
            raise ValueError("pattern must not be empty")
        return await asyncio.to_thread(
            self._grep_index_sync,
            session_id,
            pattern,
            regex,
            segment_id,
            max(1, max_results),
            max(64, excerpt_chars),
        )

    async def list_catalog(
        self, session_id: str, *, limit: int = 50
    ) -> tuple[CompactionCatalogEntry, ...]:
        return await asyncio.to_thread(
            self._list_catalog_sync, session_id, max(1, limit)
        )

    async def list_refs(
        self, session_id: str
    ) -> tuple[CompactionSegmentRef, ...]:
        return tuple(
            entry.ref
            for entry in await self.list_catalog(session_id, limit=2_147_483_647)
        )

    async def diagnostics(
        self, session_id: str
    ) -> CompactionArchiveDiagnostics:
        return await asyncio.to_thread(self._reconcile_index_sync, session_id)

    def _find_record_sync(
        self, session_id: str, segment_id: str
    ) -> tuple[CompactionSegmentRef, tuple[Message, ...]] | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        blobs = FileBlobStore(self._dir(session_id))
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    data = json.loads(raw)
                    if data.get("_kind") != "compaction_segment":
                        continue
                    ref_data = data["ref"]
                    msg_data = data["messages"]
                    if not isinstance(ref_data, dict) or not isinstance(msg_data, list):
                        continue
                    ref = _ref_from_dict(ref_data)
                    if ref.session_id != session_id:
                        continue
                    if ref.segment_id != segment_id:
                        continue
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                try:
                    messages = tuple(
                        msg_from_dict(item, blobs=blobs)
                        for item in msg_data
                        if isinstance(item, dict)
                    )
                    return ref, messages
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"archive segment unreadable: {segment_id}"
                    ) from exc
        return None

    @staticmethod
    def _ref_from_row(row: sqlite3.Row) -> CompactionSegmentRef:
        return CompactionSegmentRef(
            session_id=str(row["session_id"]),
            segment_id=str(row["segment_id"]),
            kind=str(row["kind"]),
            created_at=float(row["created_at"]),
            turn_start=int(row["turn_start"]),
            turn_end=int(row["turn_end"]),
            message_count=int(row["message_count"]),
            content_sha256=str(row["content_sha256"]),
        )

    def _search_index_sync(
        self, session_id: str, query: str, max_results: int, excerpt_chars: int
    ) -> tuple[CompactionSearchHit, ...]:
        self._reconcile_index_sync(session_id)
        normalised = _normalise(query)
        hits: list[CompactionSearchHit] = []
        seen: set[str] = set()
        with self._open_index(session_id) as connection:
            connection.row_factory = sqlite3.Row
            exact_rows = connection.execute(
                """
                SELECT ? AS session_id, *,
                       ((length(search_normalized) -
                         length(replace(search_normalized, ?, ''))) /
                        max(length(?), 1)) AS occurrence_count
                FROM segments
                WHERE instr(search_normalized, ?) > 0
                ORDER BY occurrence_count DESC, created_at DESC LIMIT ?
                """,
                (
                    session_id,
                    normalised,
                    normalised,
                    normalised,
                    max(200, max_results * 50),
                ),
            ).fetchall()
            boundary_pattern = re.compile(
                rf"(?<!\w){re.escape(normalised)}(?!\w)"
            )
            ranked_exact = sorted(
                exact_rows,
                key=lambda row: (
                    0
                    if boundary_pattern.search(str(row["search_normalized"]))
                    else 1,
                    -int(row["occurrence_count"]),
                    -float(row["created_at"]),
                ),
            )
            for row in ranked_exact[:max_results]:
                text = str(row["search_text"])
                normalised_text = _normalise(text)
                boundary_match = boundary_pattern.search(normalised_text)
                offset = (
                    boundary_match.start()
                    if boundary_match is not None
                    else normalised_text.find(normalised)
                )
                start = max(0, offset - excerpt_chars // 2)
                ref = self._ref_from_row(row)
                hits.append(
                    CompactionSearchHit(
                        ref=ref,
                        excerpt=text[start : start + excerpt_chars],
                        offset=max(0, offset),
                        match_kind=(
                            "literal" if boundary_match is not None else "substring"
                        ),
                    )
                )
                seen.add(ref.segment_id)
            if len(hits) < max_results:
                tokens = [
                    token
                    for token in re.findall(r"[\w./:+-]+", normalised)
                    if token
                ]
                for operator in (" AND ", " OR "):
                    if not tokens or len(hits) >= max_results:
                        break
                    match_query = operator.join(
                        f'"{token.replace(chr(34), chr(34) * 2)}"'
                        for token in tokens
                    )
                    try:
                        rows = connection.execute(
                            """
                            SELECT ? AS session_id, s.*,
                                   snippet(segments_fts, 0, '[', ']', ' … ', 24) AS excerpt
                            FROM segments_fts
                            JOIN segments s ON s.rowid = segments_fts.rowid
                            WHERE segments_fts MATCH ?
                            ORDER BY bm25(segments_fts), s.created_at DESC
                            LIMIT ?
                            """,
                            (session_id, match_query, max_results * 2),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                    for row in rows:
                        ref = self._ref_from_row(row)
                        if ref.segment_id in seen:
                            continue
                        hits.append(
                            CompactionSearchHit(
                                ref=ref,
                                excerpt=str(row["excerpt"])[:excerpt_chars],
                                match_kind="fts_and" if operator.strip() == "AND" else "fts_or",
                            )
                        )
                        seen.add(ref.segment_id)
                        if len(hits) >= max_results:
                            break
        return tuple(hits)

    def _grep_index_sync(
        self,
        session_id: str,
        pattern: str,
        regex: bool,
        segment_id: str | None,
        max_results: int,
        excerpt_chars: int,
    ) -> tuple[CompactionSearchHit, ...]:
        self._reconcile_index_sync(session_id)
        try:
            compiled = re.compile(pattern, re.IGNORECASE) if regex else None
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        with self._open_index(session_id) as connection:
            connection.row_factory = sqlite3.Row
            sql = "SELECT ? AS session_id, * FROM segments"
            params: list[object] = [session_id]
            if segment_id:
                sql += " WHERE segment_id = ?"
                params.append(segment_id)
            sql += " ORDER BY created_at DESC"
            rows = connection.execute(sql, params).fetchall()
            hits: list[CompactionSearchHit] = []
            needle = _normalise(pattern)
            for row in rows:
                text = str(row["search_text"])
                if compiled is not None:
                    match = compiled.search(text)
                    offset = match.start() if match else -1
                    kind = "regex"
                else:
                    offset = _normalise(text).find(needle)
                    kind = "literal"
                if offset < 0:
                    continue
                start = max(0, offset - excerpt_chars // 2)
                hits.append(
                    CompactionSearchHit(
                        ref=self._ref_from_row(row),
                        excerpt=text[start : start + excerpt_chars],
                        offset=offset,
                        match_kind=kind,
                    )
                )
                if len(hits) >= max_results:
                    break
            return tuple(hits)

    def _list_catalog_sync(
        self, session_id: str, limit: int
    ) -> tuple[CompactionCatalogEntry, ...]:
        self._reconcile_index_sync(session_id)
        with self._open_index(session_id) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT ? AS session_id, * FROM segments ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return tuple(
                CompactionCatalogEntry(
                    ref=self._ref_from_row(row),
                    tool_names=tuple(json.loads(str(row["tool_names"]))),
                    paths=tuple(json.loads(str(row["paths"]))),
                    commands=tuple(json.loads(str(row["commands"]))),
                    error_signatures=tuple(json.loads(str(row["error_signatures"]))),
                    preview=str(row["preview"]),
                )
                for row in rows
            )


class InMemoryCompactionArchive:
    """Ephemeral archive used by isolated sub-agents and deterministic tests."""

    def __init__(self, *, redactor: Redactor | None = None) -> None:
        self._redactor = redactor or Redactor()
        self._records: dict[str, list[tuple[CompactionSegmentRef, tuple[Message, ...]]]] = {}

    async def append(
        self,
        session_id: str,
        *,
        kind: str,
        messages: tuple[Message, ...],
        turn_start: int,
        turn_end: int,
    ) -> CompactionSegmentRef:
        redacted = tuple(self._redactor.message(message) for message in messages)
        text = _message_text(redacted)
        ref = CompactionSegmentRef(
            session_id=session_id,
            segment_id=f"seg-{uuid.uuid4().hex}",
            kind=kind,
            created_at=time.time(),
            turn_start=max(0, turn_start),
            turn_end=max(turn_start, turn_end),
            message_count=len(redacted),
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        self._records.setdefault(session_id, []).append((ref, redacted))
        return ref

    async def read(
        self,
        session_id: str,
        segment_id: str,
        *,
        offset: int = 0,
        max_chars: int = 8_000,
    ) -> CompactionSlice:
        for ref, messages in self._records.get(session_id, []):
            if ref.segment_id == segment_id:
                text = _message_text(messages)
                page = text[offset : offset + max_chars]
                next_offset = offset + len(page) if offset + len(page) < len(text) else None
                return CompactionSlice(ref, page, offset, next_offset, next_offset is not None)
        raise KeyError(f"compaction segment not found: {segment_id}")

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        max_results: int = 5,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]:
        needle = _normalise(query)
        if not needle:
            raise ValueError("query must not be empty")
        hits: list[CompactionSearchHit] = []
        for ref, messages in reversed(self._records.get(session_id, [])):
            text = _message_text(messages)
            normalised = _normalise(text)
            index = normalised.find(needle)
            kind = "literal"
            if index < 0:
                tokens = [token for token in re.findall(r"[\w./:+-]+", needle) if token]
                index = 0 if tokens and all(token in normalised for token in tokens) else -1
                kind = "token_and"
            if index >= 0:
                start = max(0, index - excerpt_chars // 2)
                hits.append(
                    CompactionSearchHit(
                        ref,
                        text[start : start + excerpt_chars],
                        index,
                        kind,
                    )
                )
            if len(hits) >= max(1, max_results):
                break
        return tuple(hits)

    async def grep(
        self,
        session_id: str,
        pattern: str,
        *,
        regex: bool = False,
        segment_id: str | None = None,
        max_results: int = 20,
        excerpt_chars: int = 500,
    ) -> tuple[CompactionSearchHit, ...]:
        if not pattern.strip():
            raise ValueError("pattern must not be empty")
        try:
            compiled = re.compile(pattern, re.IGNORECASE) if regex else None
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        hits: list[CompactionSearchHit] = []
        for ref, messages in reversed(self._records.get(session_id, [])):
            if segment_id and ref.segment_id != segment_id:
                continue
            text = _message_text(messages)
            if compiled is not None:
                match = compiled.search(text)
                offset = match.start() if match else -1
                kind = "regex"
            else:
                offset = _normalise(text).find(_normalise(pattern))
                kind = "literal"
            if offset < 0:
                continue
            start = max(0, offset - excerpt_chars // 2)
            hits.append(
                CompactionSearchHit(
                    ref, text[start : start + excerpt_chars], offset, kind
                )
            )
            if len(hits) >= max(1, max_results):
                break
        return tuple(hits)

    async def list_catalog(
        self, session_id: str, *, limit: int = 50
    ) -> tuple[CompactionCatalogEntry, ...]:
        return tuple(
            _catalog(ref, messages)
            for ref, messages in reversed(self._records.get(session_id, []))
        )[: max(1, limit)]

    async def list_refs(
        self, session_id: str
    ) -> tuple[CompactionSegmentRef, ...]:
        return tuple(ref for ref, _ in self._records.get(session_id, []))

    async def diagnostics(
        self, session_id: str
    ) -> CompactionArchiveDiagnostics:
        return CompactionArchiveDiagnostics(
            indexed_segments=len(self._records.get(session_id, []))
        )


__all__ = ["FileCompactionArchive", "InMemoryCompactionArchive"]
