"""TraceStore — persist a TraceModel to disk (docs/reference/events-and-storage.md §4.3/§4.10).

Stored separately from the session JSONL (P2): each session gets its own
directory ``<base>/<sid>/`` with two files:

- ``trace.jsonl`` — incremental, one ``TurnRecord`` per line (crash-safe), with a
  ``_header`` first line carrying the trace metadata + ``system_prompts`` dedup
  pool (O10/O11).
- ``trace.json`` — atomic final snapshot of the full ``TraceModel`` (tmp→rename).

``load`` prefers the final JSON; if it is missing/corrupt it falls back to
``rebuild_from_jsonl`` which tolerates bad lines (skip + count) and yields a
best-effort model marked ``end_reason="crash"``.
"""

from __future__ import annotations

import json
import fcntl
import os
import uuid
from pathlib import Path

from autoverse.infra.observ.model import (
    TraceModel,
    TurnRecord,
    CompactionSegment,
    _seg_from_dict,
    _turn_from_dict,
    _trace_from_dict,
)


class TraceStore:
    def __init__(self, base_dir: Path | str, *, fsync: bool = False) -> None:
        self._base = Path(base_dir)
        self._fsync = fsync
        # Per-session record of the dedup-pool keys already persisted to a
        # ``_header`` line, so later-seen prompts/tool sets trigger a header
        # delta line instead of being silently lost on crash.
        self._persisted_prompts: dict[str, set[str]] = {}
        self._persisted_schemas: dict[str, set[str]] = {}

    def _dir(self, sid: str) -> Path:
        return self._base / sid

    # ── incremental write ────────────────────────────────────────────────────

    def append_turn(self, model: TraceModel, turn: TurnRecord) -> None:
        """Append one turn as a JSONL line; write the header line first.

        The ``system_prompts``/``tool_schemas`` dedup pools grow lazily as new
        prompts/tool sets appear in later turns. A first-line-only header would
        leave anything first seen after turn 0 unrecoverable after a crash
        (docs/reference/events-and-storage.md §4.9): ``RequestSnapshot.system_sha256`` would point at
        a missing pool entry. So whenever the pool has grown since the last
        header we persist, we append an incremental ``_header`` line; rebuild
        merges every header line it sees.
        """
        d = self._dir(model.session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "trace.jsonl"
        cur_prompts = set(model.system_prompts)
        cur_schemas = set(model.tool_schemas)
        lock_path = d / "trace.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                write_header = not path.exists() or path.stat().st_size == 0
                delta_header = not write_header and (
                    not cur_prompts
                    <= self._persisted_prompts.get(model.session_id, set())
                    or not cur_schemas
                    <= self._persisted_schemas.get(model.session_id, set())
                )
                with path.open("a", encoding="utf-8") as handle:
                    if write_header or delta_header:
                        handle.write(
                            json.dumps(
                                {"_header": self._header(model)}, ensure_ascii=False
                            )
                            + "\n"
                        )
                        self._persisted_prompts[model.session_id] = cur_prompts
                        self._persisted_schemas[model.session_id] = cur_schemas
                    handle.write(json.dumps(turn.to_dict(), ensure_ascii=False) + "\n")
                    if self._fsync:
                        os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def append_compaction(
        self, model: TraceModel, segment: CompactionSegment
    ) -> None:
        """Persist a compaction reference immediately for crash recovery."""
        directory = self._dir(model.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "trace.jsonl"
        lock_path = directory / "trace.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                write_header = not path.exists() or path.stat().st_size == 0
                with path.open("a", encoding="utf-8") as handle:
                    if write_header:
                        handle.write(
                            json.dumps(
                                {"_header": self._header(model)}, ensure_ascii=False
                            )
                            + "\n"
                        )
                    handle.write(
                        json.dumps(
                            {"_compaction": segment.to_dict()}, ensure_ascii=False
                        )
                        + "\n"
                    )
                    if self._fsync:
                        os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    async def finalize(self, model: TraceModel) -> None:
        """Atomically snapshot the complete incremental trace.

        Rebuild under the same cross-process lock used by appenders so one
        process cannot publish a newer but incomplete local snapshot over turns
        another frontend already appended.
        """
        d = self._dir(model.session_id)
        d.mkdir(parents=True, exist_ok=True)
        lock_path = d / "trace.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                rebuilt = await self.rebuild_from_jsonl(model.session_id)
                if rebuilt is not None:
                    model.turns = rebuilt.turns
                    model.compaction_segments = rebuilt.compaction_segments
                    model.system_prompts.update(rebuilt.system_prompts)
                    model.tool_schemas.update(rebuilt.tool_schemas)
                data = json.dumps(model.to_dict(), indent=2, ensure_ascii=False)
                tmp = d / f".trace.{uuid.uuid4().hex}.tmp"
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(d / "trace.json")
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # ── read ─────────────────────────────────────────────────────────────────

    async def load(self, sid: str) -> TraceModel | None:
        final = self._dir(sid) / "trace.json"
        incremental = self._dir(sid) / "trace.jsonl"
        if final.exists() and (
            not incremental.exists()
            or final.stat().st_mtime_ns >= incremental.stat().st_mtime_ns
        ):
            try:
                return _trace_from_dict(json.loads(final.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass  # corrupt final snapshot → fall back to jsonl rebuild
        return await self.rebuild_from_jsonl(sid)

    async def list_sessions(self) -> list[str]:
        if not self._base.exists():
            return []
        return sorted(
            p.name
            for p in self._base.iterdir()
            if p.is_dir() and ((p / "trace.json").exists() or (p / "trace.jsonl").exists())
        )

    async def list_children(self, parent_sid: str) -> list[str]:
        out: list[str] = []
        for sid in await self.list_sessions():
            model = await self.load(sid)
            if model is not None and model.parent is not None:
                if model.parent.parent_session_id == parent_sid:
                    out.append(sid)
        return out

    async def rebuild_from_jsonl(self, sid: str) -> TraceModel | None:
        path = self._dir(sid) / "trace.jsonl"
        if not path.exists():
            return None
        header: dict[str, object] = {}
        merged_prompts: dict[str, str] = {}
        merged_schemas: dict[str, list[str]] = {}
        turns: list[TurnRecord] = []
        compactions: list[CompactionSegment] = []
        skipped = 0
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(obj, dict) and "_header" in obj:
                    hdr = obj["_header"]
                    if isinstance(hdr, dict):
                        header = hdr
                        # Merge dedup pools across every header line so prompts /
                        # tool sets first seen in later turns survive the rebuild
                        # (docs/reference/events-and-storage.md §4.9).
                        prompts = hdr.get("system_prompts")
                        if isinstance(prompts, dict):
                            for k, v in prompts.items():
                                merged_prompts[str(k)] = str(v)
                        schemas = hdr.get("tool_schemas")
                        if isinstance(schemas, dict):
                            for k, v in schemas.items():
                                if isinstance(v, list):
                                    merged_schemas[str(k)] = [str(x) for x in v]
                    continue
                if isinstance(obj, dict) and "_compaction" in obj:
                    raw_segment = obj["_compaction"]
                    if isinstance(raw_segment, dict):
                        try:
                            compactions.append(_seg_from_dict(raw_segment))
                        except (KeyError, TypeError, ValueError):
                            skipped += 1
                    continue
                if isinstance(obj, dict):
                    try:
                        turns.append(_turn_from_dict(obj))
                    except (KeyError, TypeError, ValueError):
                        skipped += 1
        model = _trace_from_dict(header) if header else TraceModel.new(sid, "")
        model.turns = turns
        model.compaction_segments = compactions
        model.system_prompts = merged_prompts
        model.tool_schemas = merged_schemas
        if model.end_reason == "":
            model.end_reason = "crash"
        return model

    def _header(self, model: TraceModel) -> dict[str, object]:
        hdr = model.header_dict()
        hdr["system_prompts"] = dict(model.system_prompts)
        hdr["tool_schemas"] = {k: list(v) for k, v in model.tool_schemas.items()}
        return hdr


__all__ = ["TraceStore"]
