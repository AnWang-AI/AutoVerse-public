"""Isolator Port — workspace isolation abstraction (docs/reference/subagents.md §3.1, authoritative).

This module is the *authoritative definition site* for the ``Isolator`` family
(``contracts.md`` §0 only registers a pointer here). ``Isolator`` models exactly
one semantics — git worktree isolation — used by three orthogonal orchestrators
(L1 sub-agent / L2 user tool / L3 session), differing only in ``IsoSpec.lifecycle``
and ``FinalizeAction`` (docs/reference/subagents.md §1.2).

There is deliberately no "none" mode: *not isolating* means not calling
``scope()`` at all (docs/reference/subagents.md §3.1). ``scope`` is fail-closed — a creation
failure raises :class:`IsolationError`, never silently degrades to ``base_cwd``
(docs/reference/subagents.md §3.2).

``ports`` depends only on ``types``/stdlib (import-linter contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncContextManager, Literal, Protocol, runtime_checkable


class IsolationError(RuntimeError):
    """Raised when isolation cannot be established. Fail-closed (docs/reference/subagents.md §3.2).

    The orchestrator must NOT catch this to degrade to the un-isolated cwd; the
    whole point of isolation is that writes which should stay inside the box do
    not leak out. A creation failure therefore terminates the run.
    """


@dataclass(frozen=True)
class IsoSpec:
    """The intent to isolate. ``lifecycle`` decides who reclaims (the three layers)."""

    slug: str | None = None  # human-readable name; None → auto-generated
    base_ref: str = "HEAD"  # commit/ref to branch from
    lifecycle: Literal["turn", "session", "manual"] = "turn"


@dataclass
class IsoHandle:
    """Live reference to an isolation box; ``cwd`` is where tools should run."""

    cwd: Path
    branch: str | None = None
    base_commit: str | None = None
    # Main repository root this worktree was branched from. ToolContext uses this
    # to remap absolute source-repo paths into the isolated worktree.
    source_root: Path | None = None


class FinalizeAction(Enum):
    """What happens to the changes when the box is finalized (docs/reference/subagents.md §3.1)."""

    AUTOCOMMIT = "autocommit"  # commit changes to the branch (for parent to merge)
    KEEP = "keep"  # keep worktree + branch, no commit (manual follow-up)
    DISCARD = "discard"  # discard changes + remove worktree
    MERGE = "merge"  # commit, merge branch back into base, then remove worktree
    #                  (fail-safe: a conflict / dirty base aborts and keeps the
    #                  branch for a manual merge — never leaves base half-merged)


@dataclass(frozen=True)
class IsoResult:
    """Structured outcome of finalize (P5: always actionable)."""

    branch: str | None
    commit: str | None  # AUTOCOMMIT sha, or the MERGE commit on base, if any
    removed: bool  # whether the worktree was removed
    note: str = ""


class WorkspaceMode(Enum):
    """Session-level workspace strategy (docs/reference/subagents.md §6.2; L3)."""

    SHARED = "shared"  # all sessions share cwd (default; backward compatible)
    SESSION_WORKTREE = "worktree"  # one worktree + branch per session


@dataclass
class WorkspaceSpec:
    """Declarative L3 workspace policy (docs/reference/subagents.md §6.2)."""

    mode: WorkspaceMode = WorkspaceMode.SHARED
    repo_root: Path | None = None
    branch_prefix: str = "autoverse/session"
    base_ref: str = "HEAD"
    on_session_end: Literal["keep", "merge", "discard"] = "keep"


@runtime_checkable
class Isolator(Protocol):
    """The isolation capability (docs/reference/subagents.md §3.1). Three orthogonal actions.

    - ``scope`` builds the box and guarantees reclamation (via ``__aexit__``).
    - ``repo_root`` resolves which git repository a cwd belongs to, so higher
      layers can choose the right per-repo worktree before creating/reusing one.
    - ``audit`` read-only checks the box's changes against a policy (no mutation).
    - ``has_changes`` read-only "is there any work to lose?" probe — uncommitted
      changes (tracked/untracked/staged) or commits beyond ``base_commit``. Lets
      an orchestrator skip a keep/merge/discard decision for a pristine box and
      tear it down silently. Fail-safe: implementations report ``True`` on any
      uncertainty so work is never silently discarded (P5, docs/reference/subagents.md §5a.3).
    - ``has_uncommitted_changes`` is narrower: it ignores committed work past
      ``base_commit`` and only checks whether KEEP would strand uncommitted files.
    - ``finalize`` decides the fate of the changes (commit / keep / discard /
      merge-back). ``MERGE`` commits the box, merges its branch into the base
      work-tree, and removes the box on success; a conflict or a dirty base is
      fail-safe (aborts the merge, keeps the branch — never half-merges base).
    """

    def scope(
        self, spec: IsoSpec, base_cwd: Path
    ) -> AsyncContextManager[IsoHandle]: ...

    async def repo_root(self, base_cwd: Path) -> Path: ...

    async def audit(
        self, iso: IsoHandle, policy: str | None
    ) -> tuple[bool, str | None]: ...

    async def has_changes(self, iso: IsoHandle) -> bool: ...

    async def has_uncommitted_changes(self, iso: IsoHandle) -> bool: ...

    async def finalize(self, iso: IsoHandle, action: FinalizeAction) -> IsoResult: ...


@runtime_checkable
class WorktreeRegistry(Protocol):
    """Explicit per-session/process worktree state — no global singleton (P4, §3.4)."""

    def register(self, handle: IsoHandle, lifecycle: str) -> None: ...

    def active_for_session(self, session_id: str) -> IsoHandle | None: ...  # L3

    def active_manual(
        self, session_id: str, source_root: Path | None = None
    ) -> IsoHandle | None: ...  # L2

    def list_orphans(self, repo_root: Path) -> list[Path]: ...  # crash leftovers

    async def cleanup(self, handle: IsoHandle) -> None: ...


__all__ = [
    "IsolationError",
    "IsoSpec",
    "IsoHandle",
    "FinalizeAction",
    "IsoResult",
    "WorkspaceMode",
    "WorkspaceSpec",
    "Isolator",
    "WorktreeRegistry",
]
