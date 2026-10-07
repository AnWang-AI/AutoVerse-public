"""File and in-memory adapters for atomic session turn indices."""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path


class FileTurnIndexStore:
    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    async def reserve(self, session_id: str, *, minimum: int = 0) -> int:
        return await asyncio.to_thread(self._reserve_sync, session_id, minimum)

    def _reserve_sync(self, session_id: str, minimum: int) -> int:
        if (
            not session_id
            or session_id in {".", ".."}
            or Path(session_id).name != session_id
            or "/" in session_id
            or "\\" in session_id
        ):
            raise ValueError("session_id must be a single safe path component")
        path = self._base / f"{session_id}.counter"
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                raw = handle.read().strip()
                current = int(raw) if raw.isdigit() else 0
                reserved = max(current, max(0, minimum))
                handle.seek(0)
                handle.truncate()
                handle.write(str(reserved + 1))
                handle.flush()
                os.fsync(handle.fileno())
                os.chmod(path, 0o600)
                return reserved
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class InMemoryTurnIndexStore:
    def __init__(self) -> None:
        self._next: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, session_id: str, *, minimum: int = 0) -> int:
        async with self._lock:
            reserved = max(self._next.get(session_id, 0), max(0, minimum))
            self._next[session_id] = reserved + 1
            return reserved


__all__ = ["FileTurnIndexStore", "InMemoryTurnIndexStore"]
