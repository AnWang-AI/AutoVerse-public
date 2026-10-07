"""FileTracker — session-level read/write bookkeeping (docs/reference/tools.md §8.1).

Enforces two invariants for write/edit tools:
  1. "未读先写" (write-before-read): a file must be read before it is
     overwritten, so the model cannot blindly clobber unseen content.
  2. "读后被改" (stale read): a file changed on disk since the last read must
     be re-read before being written.

Also vends per-path asyncio locks (reference-counted, GC'd on release) so
concurrent edits to the same file serialise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class FileTracker:
    def __init__(self) -> None:
        self._read_mtimes: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refcount: dict[str, int] = {}

    @staticmethod
    def _key(path: Path) -> str:
        return str(path)

    def set_read_mtime(self, path: Path, mtime: float) -> None:
        self._read_mtimes[self._key(path)] = mtime

    def get_last_read_mtime(self, path: Path) -> float | None:
        return self._read_mtimes.get(self._key(path))

    def was_read_before_write(
        self, path: Path, cur_mtime: float | None
    ) -> tuple[bool, str]:
        """Return (ok, reason). ok=True means the write may proceed.

        - Not read at all -> blocked ("未读先写").
        - Read, but mtime on disk advanced past the recorded read -> blocked
          ("读后被改"). A brand-new file (cur_mtime is None) is allowed once read
          tracking exists, but if never read it is still blocked.
        """
        last = self.get_last_read_mtime(path)
        if last is None:
            return (
                False,
                f"File has not been read in this session: {self._key(path)}. "
                "Read it before writing.",
            )
        if cur_mtime is not None and cur_mtime > last:
            return (
                False,
                f"File changed on disk since last read: {self._key(path)}. "
                "Re-read it before writing.",
            )
        return (True, "")

    @asynccontextmanager
    async def file_lock(self, path: Path) -> AsyncIterator[None]:
        """Per-path async lock with reference-counted lifecycle (GC on release)."""
        key = self._key(path)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._lock_refcount[key] = self._lock_refcount.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_refcount[key] -= 1
            if self._lock_refcount[key] <= 0:
                self._lock_refcount.pop(key, None)
                self._locks.pop(key, None)

    def snapshot_before_write(self, path: Path) -> bool:
        """Hook for snapshot integration (wired in a later phase). Stub returns True."""
        return True


__all__ = ["FileTracker"]
