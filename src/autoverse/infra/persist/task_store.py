"""TaskStore — session-isolated todos + per-task progress journal (§6/§6.1).

- ``replace``/``list`` store the whole todo list per session (todo_write semantics
  is "submit the full list"); storage is an atomic whole-file rewrite.
- ``append_progress``/``read_progress`` maintain an append-only free-text journal
  per task (``tasks/<sid>/<task_id>/progress.md``) for checkpoint aggregation.

Both dimensions are session-isolated and orthogonal (P2). Consistency follows
§4.3 (write disk atomically, then update memory; no double-write).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List as _List
from typing import Literal

TaskStatus = Literal["pending", "in_progress", "completed"]
TaskPriority = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class TodoItem:
    id: str
    content: str
    status: TaskStatus
    priority: TaskPriority = "medium"


class TaskStore:
    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir) / "tasks"
        self._base.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, list[TodoItem]] = {}

    def _path(self, sid: str) -> Path:
        return self._base / f"{sid}.json"

    async def list(self, session_id: str) -> "_List[TodoItem]":
        if session_id in self._cache:
            return list(self._cache[session_id])
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = [_item_from_dict(d) for d in raw if isinstance(d, dict)]
        self._cache[session_id] = items
        return list(items)

    async def replace(self, session_id: str, items: "_List[TodoItem]") -> None:
        body = json.dumps([_item_to_dict(i) for i in items], ensure_ascii=False)
        path = self._path(session_id)
        tmp = self._base / f".{session_id}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)  # atomic write first, then update memory (§4.3)
        self._cache[session_id] = list(items)

    def has_pending(self, session_id: str | None = None) -> bool:
        """Sync snapshot of whether any tracked todo is still open (CompletionGate).

        Reads the in-memory cache only (the gate runs in the hot end-of-turn path
        and must not block on disk). A todo is "pending" unless ``completed``.
        ``session_id=None`` checks every cached session — in practice one engine
        owns one session, so this resolves to "this session has open todos".
        """
        sessions = [session_id] if session_id is not None else list(self._cache)
        for sid in sessions:
            for item in self._cache.get(sid) or []:
                if item.status != "completed":
                    return True
        return False

    # ── progress journal (§6.1) ──────────────────────────────────────────────

    def _progress_path(self, sid: str, task_id: str) -> Path:
        return self._base / sid / task_id / "progress.md"

    async def append_progress(self, session_id: str, task_id: str, entry: str) -> None:
        """Append a timestamped free-text progress entry (append-only)."""
        path = self._progress_path(session_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- [{ts}] {entry}\n")

    async def read_progress(self, session_id: str, task_id: str) -> str:
        path = self._progress_path(session_id, task_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def _item_to_dict(i: TodoItem) -> dict[str, object]:
    return {
        "id": i.id,
        "content": i.content,
        "status": i.status,
        "priority": i.priority,
    }


def _item_from_dict(d: dict[str, object]) -> TodoItem:
    status = d.get("status", "pending")
    priority = d.get("priority", "medium")
    return TodoItem(
        id=str(d.get("id", "")),
        content=str(d.get("content", "")),
        status=status if status in ("pending", "in_progress", "completed") else "pending",
        priority=priority if priority in ("low", "medium", "high") else "medium",
    )


__all__ = ["TaskStore", "TodoItem", "TaskStatus", "TaskPriority"]
