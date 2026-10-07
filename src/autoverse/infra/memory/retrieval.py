"""Optional FTS5 retrieval memory helper (docs/reference/config.md).

Relevance-based memory injection: instead of full truncation, index memory
files into a SQLite FTS5 table and return the top-k snippets for the current
task query. This is an orthogonal helper beside the full assembly path in
:mod:`autoverse.infra.memory.loader`, NOT a replacement. The default
EngineBuilder prompt path does not instantiate this backend yet; callers must
explicitly wire it.

FTS5 availability is guarded: if the runtime SQLite has no FTS5 module the
provider reports itself unavailable and callers fail-open to full assembly.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

RetrievalScope = Literal["global", "project", "subdir", "session", "task"]


def _detect_fts5() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


FTS5_AVAILABLE = _detect_fts5()

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
#: Keep results scoring at least this fraction of the top hit (relative floor,
#: §3.5a) — OR-joined queries otherwise match docs that share one common term.
_RELATIVE_FLOOR = 0.35


@dataclass(frozen=True)
class RetrievedMemory:
    """One relevance-ranked memory hit."""

    source: str
    snippet: str
    score: float
    scope: RetrievalScope
    scope_id: str = ""


def should_use_retrieval(
    *,
    total_chars: int,
    max_total_chars: int,
    retrieval_enabled: bool,
) -> bool:
    """Decide whether a caller should engage retrieval.

    Retrieval is OFF by default: it engages only when explicitly opted in, or
    when the full memory would exceed ``max_total_chars``. ``max_total_chars``
    of 0 means "no cap", so size alone never triggers retrieval.
    """
    if retrieval_enabled:
        return True
    if max_total_chars <= 0:
        return False
    return total_chars > max_total_chars


def _build_match_query(query: str) -> str:
    """Phrase-quote each alnum token and OR-join them.

    Avoids a bare user string tripping FTS5 syntax. Returns ``""`` when the
    query has no usable tokens (caller should skip the search).
    """
    tokens = _TOKEN_RE.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{token}"' for token in tokens)


class Fts5RetrievalMemory:
    """SQLite-FTS5 retrieval backend (default :class:`RetrievalMemory` impl).

    The index is a rebuildable derived cache of the on-disk memory files — the
    disk files remain the truth source (P1). ``reconcile`` rebuilds the index
    from the supplied documents. Use an in-memory DB (``":memory:"``) for tests.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        if not FTS5_AVAILABLE:
            raise RuntimeError(
                "SQLite FTS5 is not available in this runtime; retrieval memory "
                "is disabled and callers fall back to full assembly."
            )
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "source, snippet, scope, scope_id)"
        )
        self._conn.commit()

    def reconcile(self, documents: Iterable[Mapping[str, str]]) -> tuple[int, int]:
        """Rebuild the index from ``documents``; returns ``(indexed, pruned)``.

        Each document is a mapping with ``source``/``snippet``/``scope`` (and an
        optional ``scope_id``). Full rebuild = prune everything, then re-index.
        """
        pruned = self._conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
        self._conn.execute("DELETE FROM memory_fts")
        indexed = 0
        for doc in documents:
            self._conn.execute(
                "INSERT INTO memory_fts(source, snippet, scope, scope_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    doc["source"],
                    doc["snippet"],
                    doc.get("scope", "project"),
                    doc.get("scope_id", ""),
                ),
            )
            indexed += 1
        self._conn.commit()
        return indexed, int(pruned)

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        scope_id: str | None = None,
        limit: int = 10,
    ) -> list[RetrievedMemory]:
        match = _build_match_query(query)
        if not match:
            return []
        sql = (
            "SELECT source, snippet, scope, scope_id, bm25(memory_fts) AS rank "
            "FROM memory_fts WHERE memory_fts MATCH ?"
        )
        params: list[object] = [match]
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if scope_id is not None:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []
        # bm25 returns lower = better (negative); convert to positive scores.
        scored = [(row, -float(row[4])) for row in rows]
        top = scored[0][1]
        floor = top * _RELATIVE_FLOOR if top > 0 else float("-inf")
        results: list[RetrievedMemory] = []
        for index, (row, score) in enumerate(scored):
            if index > 0 and score < floor:
                continue
            results.append(
                RetrievedMemory(
                    source=str(row[0]),
                    snippet=str(row[1]),
                    score=score,
                    scope=_coerce_scope(str(row[2])),
                    scope_id=str(row[3]),
                )
            )
        return results

    def close(self) -> None:
        self._conn.close()


def _coerce_scope(value: str) -> RetrievalScope:
    if value in ("global", "project", "subdir", "session", "task"):
        return value  # type: ignore[return-value]
    return "project"


__all__ = [
    "FTS5_AVAILABLE",
    "Fts5RetrievalMemory",
    "RetrievedMemory",
    "should_use_retrieval",
]
