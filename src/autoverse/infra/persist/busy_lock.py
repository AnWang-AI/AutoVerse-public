"""BusyLock — cross-process "session busy" indicator (docs/reference/events-and-storage.md §8).

Writes ``<base_dir>/<session_id>.busy`` containing the holder pid so external
processes (WebUI / gateway) can detect that a session is being processed. It is
*not* a write mutex (that is the rewrite flock in JSONLStore); it is a hint with
stale-lock self-healing: :meth:`is_busy` probes the recorded pid with
``os.kill(pid, 0)`` — if the holder is dead (``ProcessLookupError``) the stale
lock is reclaimed and ``False`` is returned (D5).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class BusyLock:
    def __init__(self, base_dir: Path | str) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        return self._base / f"{sid}.busy"

    def acquire(self, sid: str, *, frontend: str = "") -> None:
        """Best-effort: write the busy marker with this process's pid."""
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "frontend": frontend,
        }
        try:
            self._path(sid).write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass  # busy lock is a hint; never crash on write failure

    def release(self, sid: str) -> None:
        """Remove the busy marker (register to atexit / finally)."""
        try:
            self._path(sid).unlink(missing_ok=True)
        except OSError:
            pass

    def is_busy(self, sid: str) -> bool:
        """True only if the marker exists *and* its pid is a live process."""
        path = self._path(sid)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            # Corrupt marker: reclaim and treat as free.
            self.release(sid)
            return False
        if not self._pid_alive(pid):
            self.release(sid)  # stale lock self-heal (holder dead)
            return False
        return True

    def holder(self, sid: str) -> dict[str, object] | None:
        """Return the raw marker payload, or None if absent/unreadable."""
        path = self._path(sid)
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists but owned by another user → busy
        return True


__all__ = ["BusyLock"]
