"""ToolContext — per-dispatch execution context handed to every tool (docs/reference/tools.md §2).

Carries the cwd, the permission port (tools NEVER call it — see P2; the
dispatcher does the gating), the session-level FileTracker, the abort signal,
the event sink, the AppContext (forward-referenced to keep core ⊥ app), and an
optional diagnostics seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from autoverse.core.tools.pathresolve import resolve_in_workspace as _resolve_in_workspace
from autoverse.core.tools.pathresolve import is_within as _is_within
from autoverse.core.tools.pathresolve import resolve_path as _resolve_path
from autoverse.core.tools.tracker import FileTracker
from autoverse.ports import EventSink, PermissionPort
from autoverse.types.abort import AbortSignal

if TYPE_CHECKING:
    from autoverse.app.context import AppContext
    from autoverse.ports.diagnostics import DiagnosticsPort


@dataclass
class ToolContext:
    cwd: Path
    permissions: PermissionPort
    tracker: FileTracker
    signal: AbortSignal
    events: EventSink
    app: "AppContext"
    diagnostics: "DiagnosticsPort | None" = None
    isolation_root: Path | None = None
    isolation_source_root: Path | None = None

    def resolve_path(self, file_path: str) -> Path:
        """Resolve a (possibly relative) path against the tool cwd."""
        return self._map_isolated_path(_resolve_path(self.cwd, file_path), file_path)

    def resolve_path_in_workspace(self, file_path: str) -> tuple[Path, bool]:
        """Resolve a path and report whether a relative input escaped the cwd.

        Outside isolation, absolute paths are never flagged (gated by
        capability/permissions per docs/reference/tools.md §9.2). Inside isolation, absolute
        paths must either already point at the worktree or point at the source
        repo so they can be remapped into the worktree; anything else escapes.
        """
        root = self._norm(self.isolation_root)
        if root is None:
            return _resolve_in_workspace(self.cwd, file_path)
        resolved = self.resolve_path(file_path)
        return (resolved, not _is_within(root, resolved))

    def _map_isolated_path(self, resolved: Path, raw: str) -> Path:
        root = self._norm(self.isolation_root)
        if root is None:
            return resolved
        resolved_n = self._norm(resolved) or resolved
        if _is_within(root, resolved_n):
            return resolved_n
        source = self._norm(self.isolation_source_root)
        if Path(raw).is_absolute() and source is not None and _is_within(source, resolved_n):
            rel = resolved_n.relative_to(source)
            return self._norm(root / rel) or (root / rel)
        return resolved_n

    @staticmethod
    def _norm(path: Path | None) -> Path | None:
        if path is None:
            return None
        return Path(path).expanduser().resolve(strict=False)


__all__ = ["ToolContext"]
