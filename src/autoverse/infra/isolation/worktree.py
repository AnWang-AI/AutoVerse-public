"""GitWorktreeIsolator + InMemoryWorktreeRegistry (docs/reference/subagents.md §3, authoritative impl).

A single ``Isolator`` implementation realising the worktree semantics for all
three layers (L1/L2/L3). Worktrees live under ``<repo>/.autoverse/worktrees/<slug>``
on a branch ``autoverse/wt/<slug>`` (deterministic naming, §3.3). git meta
operations on the same repo are serialised with a process-local ``asyncio.Lock``
(§6.4). ``scope`` is fail-closed: any creation failure raises
:class:`IsolationError` — never degrades to ``base_cwd`` (§3.2).

``WorktreeRegistry`` keeps explicit per-session state; no module-level singleton
(P4, §3.4).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from autoverse.core.subagent.audit import (
    POLICY_GIT_DIFF_EMPTY,
    git_diff_tracked,
    post_audit,
)
from autoverse.ports.isolation import (
    FinalizeAction,
    IsoHandle,
    IsoResult,
    IsoSpec,
    IsolationError,
)

_SLUG_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_BRANCH_PREFIX = "autoverse/wt"
_WORKTREE_SUBDIR = ".autoverse/worktrees"

# Process-wide, per-repo serialisation of mutating git *meta* operations
# (worktree add/remove, branch create/delete, commit, merge). docs/reference/subagents.md §6.4
# promises same-repo git meta ops are serialised process-locally — but each
# session builds its *own* GitWorktreeIsolator (builder.py), so a per-instance
# lock cannot serialise two sessions racing on the SAME repo (concurrent
# ``worktree add`` / ref writes → index.lock / ref races). Keying a module-level
# lock by the resolved repo root closes that gap without globally serialising
# unrelated repos. Safe to mutate without a guard: asyncio is single-threaded and
# there is no ``await`` between the get and the set below.
_REPO_LOCKS: dict[str, "asyncio.Lock"] = {}


def _repo_lock(repo_root: Path) -> "asyncio.Lock":
    key = str(Path(repo_root).resolve())
    lock = _REPO_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _REPO_LOCKS[key] = lock
    return lock


def validate_slug(slug: str) -> None:
    """Reject empty / path-traversing / oversized slugs (§3.3).

    Allows ``/``-separated segments; each segment is ``[A-Za-z0-9._-]`` only and
    no longer than 64 chars. Rejects ``.``/``..`` segments to prevent traversal.
    """
    if not slug or len(slug) > 200:
        raise IsolationError(f"invalid slug: {slug!r}")
    for segment in slug.split("/"):
        if not segment or len(segment) > 64:
            raise IsolationError(f"invalid slug segment: {segment!r}")
        if segment in (".", ".."):
            raise IsolationError(f"slug segment must not be . or ..: {slug!r}")
        if not _SLUG_SEGMENT.match(segment):
            raise IsolationError(f"slug segment has illegal chars: {segment!r}")


def branch_name(slug: str) -> str:
    """Deterministic branch name with a fixed prefix (§3.3)."""
    return f"{_BRANCH_PREFIX}/{slug}"


def worktree_path(repo_root: Path, slug: str) -> Path:
    """Deterministic worktree path under the repo (§3.3)."""
    return Path(repo_root) / _WORKTREE_SUBDIR / slug


def _auto_slug() -> str:
    return uuid.uuid4().hex[:12]


class _WorktreeState:
    """Internal bookkeeping for a created worktree (used by scope/finalize)."""

    def __init__(
        self,
        path: Path,
        branch: str,
        base: str | None,
        lifecycle: str,
        repo_root: Path | None = None,
    ) -> None:
        self.path = path
        self.branch = branch
        self.base = base
        self.lifecycle = lifecycle
        # The main work-tree root — where a MERGE-back is applied and where the
        # repo-meta lock is keyed. Stored so finalize never has to re-derive it.
        self.repo_root = repo_root


class GitWorktreeIsolator:
    """``Isolator`` over git worktrees. Process-local git serialisation."""

    def __init__(self) -> None:
        self._git_lock = asyncio.Lock()
        # Map a handle's cwd → its internal state for finalize/audit.
        self._states: dict[Path, _WorktreeState] = {}

    # ── git plumbing (serialised) ────────────────────────────────────────────
    async def _git(self, cwd: Path, *args: str) -> tuple[int, str, str]:
        async with self._git_lock:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            out, err = await proc.communicate()
            return (
                proc.returncode if proc.returncode is not None else -1,
                out.decode("utf-8", errors="replace"),
                err.decode("utf-8", errors="replace"),
            )

    async def _repo_root(self, base_cwd: Path) -> Path:
        code, out, err = await self._git(base_cwd, "rev-parse", "--show-toplevel")
        if code != 0:
            raise IsolationError(
                f"not a git repository (or git unavailable) at {base_cwd}: {err.strip()}"
            )
        return Path(out.strip())

    async def repo_root(self, base_cwd: Path) -> Path:
        """Resolve the git root for *base_cwd* without creating a worktree."""
        return await self._repo_root(Path(base_cwd))

    async def _create_worktree(self, base_cwd: Path, spec: IsoSpec) -> _WorktreeState:
        repo_root = await self._repo_root(base_cwd)
        slug = spec.slug or _auto_slug()
        validate_slug(slug)
        branch = branch_name(slug)
        wt_path = worktree_path(repo_root, slug)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Serialise the mutating meta op against any other session on this repo.
        async with _repo_lock(repo_root):
            code, _, err = await self._git(
                repo_root,
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_path),
                spec.base_ref,
            )
            if code != 0:
                raise IsolationError(f"git worktree add failed: {err.strip()}")
            # Record the branch's true starting point from the *created* worktree's
            # HEAD. This always resolves (the add just succeeded), so base_commit is
            # never None — unlike pre-resolving ``spec.base_ref`` from the main repo,
            # which could miss and leave has_changes unable to detect new commits.
            code, base_sha, _ = await self._git(wt_path, "rev-parse", "HEAD")
            base_commit = base_sha.strip() if code == 0 and base_sha.strip() else None
        return _WorktreeState(wt_path, branch, base_commit, spec.lifecycle, repo_root)

    async def _remove_worktree_locked(
        self, state: _WorktreeState, *, delete_branch: bool = False
    ) -> None:
        """Worktree (and optionally branch) removal — assumes the repo lock is held.

        Split out so MERGE can stay inside one lock acquisition (asyncio.Lock is
        NOT reentrant — calling the locking wrapper while already holding the lock
        would deadlock).
        """
        meta_cwd = state.repo_root or state.path.parent
        # Remove the worktree (force — it may hold uncommitted byproducts).
        await self._git(meta_cwd, "worktree", "remove", "--force", str(state.path))
        # DISCARD/MERGE-success also drop the branch so transient/empty boxes
        # don't accumulate ``autoverse/wt/<uuid>`` refs forever. ``-D``
        # force-deletes even unmerged commits. Stay fail-safe: a branch-delete
        # miss must not make finalize report failure. NOTE: turn-level/AUTOCOMMIT
        # cleanup passes ``delete_branch=False`` — that branch carries commits the
        # parent agent still needs to merge (docs/reference/subagents.md §4).
        if delete_branch and state.branch:
            await self._git(meta_cwd, "branch", "-D", state.branch)

    async def _cleanup_worktree(
        self, state: _WorktreeState, *, delete_branch: bool = False
    ) -> None:
        # Serialise against concurrent same-repo sessions, then delegate.
        meta_cwd = state.repo_root or state.path.parent
        async with _repo_lock(meta_cwd):
            await self._remove_worktree_locked(state, delete_branch=delete_branch)

    # ── Isolator Protocol ─────────────────────────────────────────────────────
    @asynccontextmanager
    async def scope(self, spec: IsoSpec, base_cwd: Path) -> AsyncIterator[IsoHandle]:
        # Entering scope means "isolate"; the un-isolated path never calls scope.
        try:
            state = await self._create_worktree(Path(base_cwd), spec)
        except IsolationError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-closed: never degrade
            raise IsolationError(
                f"worktree isolation failed; refusing to run un-isolated: {exc}"
            ) from exc
        handle = IsoHandle(
            cwd=state.path,
            branch=state.branch,
            base_commit=state.base,
            source_root=state.repo_root,
        )
        self._states[state.path] = state
        try:
            yield handle
        finally:
            # turn-level lifecycle reclaims here; session/manual is the orchestrator's job.
            if spec.lifecycle == "turn":
                await self._cleanup_worktree(state)
                self._states.pop(state.path, None)

    async def _audit_diff(self, cwd: Path) -> str:
        """Serialised, fail-loud ``git diff --name-only HEAD --`` stdout for *cwd*.

        Injected into core's :func:`git_diff_tracked` so the diff step is shared
        while git access stays serialised (§6.4) and failures stay fail-closed.
        """
        code, out, err = await self._git(cwd, "diff", "--name-only", "HEAD", "--")
        if code != 0:
            raise IsolationError(f"audit git diff failed: {err.strip()}")
        return out

    async def audit(self, iso: IsoHandle, policy: str | None) -> tuple[bool, str | None]:
        if policy != POLICY_GIT_DIFF_EMPTY:
            return False, None
        # Only tracked source changes count; untracked byproducts are tolerated.
        changed = await git_diff_tracked(iso.cwd, run=self._audit_diff)
        return post_audit(policy, changed)

    async def has_changes(self, iso: IsoHandle) -> bool:
        """True if the worktree has uncommitted changes or commits beyond base.

        Lets the worktree-mode orchestrator skip the keep/merge/discard prompt
        for a pristine box (nothing was done) and silently tear it down. Counts
        *any* working-tree change — tracked, staged, **and** untracked (``??``) —
        plus new commits past ``base_commit``.

        Fail-safe (P5): on any git error this returns ``True`` (assume there may
        be work to lose → caller should prompt), never silently reports "empty".
        """
        code, out, _ = await self._git(iso.cwd, "status", "--porcelain")
        if code != 0:
            return True  # cannot tell → never silently discard
        if out.strip():
            return True
        if iso.base_commit:
            code, head, _ = await self._git(iso.cwd, "rev-parse", "HEAD")
            if code == 0 and head.strip() and head.strip() != iso.base_commit:
                return True
        return False

    async def has_uncommitted_changes(self, iso: IsoHandle) -> bool:
        """True only for working-tree/staged/untracked changes.

        Unlike :meth:`has_changes`, this deliberately ignores commits beyond
        ``base_commit``. KEEP should reject only uncommitted work; a clean branch
        with already-committed work is exactly what KEEP is meant to preserve.
        """
        code, out, _ = await self._git(iso.cwd, "status", "--porcelain")
        if code != 0:
            return True  # cannot tell → ask caller to avoid claiming it is clean
        return bool(out.strip())

    async def finalize(self, iso: IsoHandle, action: FinalizeAction) -> IsoResult:
        state = self._states.get(iso.cwd)
        if action is FinalizeAction.AUTOCOMMIT:
            lock_root = state.repo_root if state and state.repo_root else iso.cwd
            async with _repo_lock(lock_root):
                await self._git(iso.cwd, "add", "-A")
                stamp = datetime.now(timezone.utc).isoformat()
                await self._git(iso.cwd, "commit", "-q", "-m", f"autoverse autocommit {stamp}")
                code, sha, _ = await self._git(iso.cwd, "rev-parse", "HEAD")
            commit = sha.strip() if code == 0 else None
            return IsoResult(branch=iso.branch, commit=commit, removed=False)
        if action is FinalizeAction.KEEP:
            # KEEP = "keep worktree + branch, no commit" (ports/isolation.py);
            # leave changes uncommitted for a manual follow-up.
            return IsoResult(branch=iso.branch, commit=None, removed=False, note="kept")
        if action is FinalizeAction.MERGE:
            return await self._finalize_merge(iso, state)
        # DISCARD
        if state is not None:
            await self._cleanup_worktree(state, delete_branch=True)
            self._states.pop(iso.cwd, None)
        return IsoResult(branch=iso.branch, commit=None, removed=True, note="discarded")

    async def _finalize_merge(
        self, iso: IsoHandle, state: _WorktreeState | None
    ) -> IsoResult:
        """Commit the box, merge its branch back into base, then remove the box.

        Fail-safe at every step (P5): if anything would corrupt or surprise the
        base work-tree we abort and KEEP the branch for a manual merge instead of
        forcing a half-merge or losing work:
          * no bookkeeping / no branch  → keep (can't safely merge)
          * nothing to merge            → remove the empty box (no-op merge)
          * base work-tree dirty        → keep (merging onto dirty base is unsafe)
          * merge conflict              → ``git merge --abort`` + keep
        Only a clean, conflict-free merge removes the worktree and its branch.
        """
        branch = state.branch if state else iso.branch
        if state is None or state.repo_root is None or not branch:
            return IsoResult(
                branch=branch,
                commit=None,
                removed=False,
                note="merge unavailable (worktree state lost); kept branch for manual merge",
            )
        repo_root = state.repo_root
        stamp = datetime.now(timezone.utc).isoformat()
        async with _repo_lock(repo_root):
            # 1. Commit any pending work in the box (empty commit is impossible →
            #    a clean tree simply leaves HEAD where it is; that's handled next).
            await self._git(iso.cwd, "add", "-A")
            await self._git(iso.cwd, "commit", "-q", "-m", f"autoverse worktree {stamp}")
            code, sha, _ = await self._git(iso.cwd, "rev-parse", "HEAD")
            commit = sha.strip() if code == 0 and sha.strip() else None

            # 2. Nothing new beyond base → tear the empty box down (no merge).
            if not commit or (iso.base_commit and commit == iso.base_commit):
                await self._remove_worktree_locked(state, delete_branch=True)
                self._states.pop(iso.cwd, None)
                return IsoResult(
                    branch=branch,
                    commit=None,
                    removed=True,
                    note="nothing to merge; removed empty worktree",
                )

            # 3. The base work-tree must be clean — never merge onto dirty state.
            #    Exclude our own ``.autoverse/worktrees`` dir: it lives *inside* the
            #    repo, so the main work-tree always sees it as an untracked path —
            #    that's infrastructure, not user dirt, and must not block the merge.
            code, st_out, _ = await self._git(
                repo_root,
                "status",
                "--porcelain",
                "-uall",
                "--",
                ".",
                f":(exclude,top){_WORKTREE_SUBDIR}",
            )
            if code != 0 or st_out.strip():
                return IsoResult(
                    branch=branch,
                    commit=commit,
                    removed=False,
                    note=f"base work-tree not clean; kept branch {branch} for manual merge",
                )

            # 4. Attempt the merge into the base's current HEAD.
            code, _, err = await self._git(
                repo_root,
                "merge",
                "--no-ff",
                "-m",
                f"Merge {branch} (autoverse worktree)",
                branch,
            )
            if code != 0:
                # Conflict / failure → leave base exactly as it was, keep the box.
                await self._git(repo_root, "merge", "--abort")
                return IsoResult(
                    branch=branch,
                    commit=commit,
                    removed=False,
                    note=f"merge conflict; aborted, kept branch {branch} for manual merge"
                    + (f" ({err.strip()})" if err.strip() else ""),
                )

            # 5. Success → record the merge commit, remove worktree + branch.
            code, msha, _ = await self._git(repo_root, "rev-parse", "HEAD")
            merge_commit = msha.strip() if code == 0 and msha.strip() else commit
            await self._remove_worktree_locked(state, delete_branch=True)
            self._states.pop(iso.cwd, None)
            return IsoResult(branch=branch, commit=merge_commit, removed=True, note="merged")

    async def prune(self, repo_root: Path) -> int:
        """Best-effort ``git worktree prune`` for a repo (P2, docs/reference/subagents.md §3.4).

        Removes only *administrative* registrations whose working directory has
        already vanished (e.g. a dir manually deleted after a crash). It is
        deliberately non-destructive to existing directories: a leftover worktree
        dir from a crash and a live worktree owned by *another* running session
        are indistinguishable from the outside (both stay registered in
        ``git worktree list``), so auto-deleting dirs would risk killing a
        concurrent session. Returns 0/1 as a crude "did it run" signal; never
        raises (pruning must not block entering the mode).
        """
        try:
            async with _repo_lock(repo_root):
                code, _, _ = await self._git(repo_root, "worktree", "prune")
            return 1 if code == 0 else 0
        except Exception:  # noqa: BLE001 — maintenance op, never fatal
            return 0


class InMemoryWorktreeRegistry:
    """Explicit per-session worktree state; no global singleton (P4, §3.4)."""

    def __init__(self) -> None:
        self._session: dict[str, IsoHandle] = {}
        self._manual: dict[tuple[str, str], IsoHandle] = {}
        self._all: list[tuple[IsoHandle, str]] = []

    @staticmethod
    def _manual_key(session_id: str, source_root: Path | None) -> tuple[str, str]:
        root = "" if source_root is None else str(Path(source_root).resolve(strict=False))
        return (session_id, root)

    @staticmethod
    def _handle_source_root(handle: IsoHandle) -> Path | None:
        root = getattr(handle, "source_root", None)
        return Path(root) if root is not None else None

    def register(self, handle: IsoHandle, lifecycle: str) -> None:
        self._all.append((handle, lifecycle))

    def register_session(self, session_id: str, handle: IsoHandle) -> None:
        self._session[session_id] = handle
        self.register(handle, "session")

    def register_manual(self, session_id: str, handle: IsoHandle) -> None:
        self._manual[self._manual_key(session_id, self._handle_source_root(handle))] = handle
        self.register(handle, "manual")

    def active_for_session(self, session_id: str) -> IsoHandle | None:
        return self._session.get(session_id)

    def active_manual(
        self, session_id: str, source_root: Path | None = None
    ) -> IsoHandle | None:
        if source_root is not None:
            return self._manual.get(self._manual_key(session_id, source_root))
        matches = [h for (sid, _), h in self._manual.items() if sid == session_id]
        return matches[0] if len(matches) == 1 else None

    def list_orphans(self, repo_root: Path) -> list[Path]:
        root = Path(repo_root) / _WORKTREE_SUBDIR
        if not root.exists():
            return []
        known = {h.cwd for h, _ in self._all}
        return [p for p in root.iterdir() if p.is_dir() and p not in known]

    async def cleanup(self, handle: IsoHandle) -> None:
        self._all = [(h, lc) for h, lc in self._all if h is not handle]
        for session_key, h in list(self._session.items()):
            if h is handle:
                self._session.pop(session_key, None)
        for manual_key, h in list(self._manual.items()):
            if h is handle:
                self._manual.pop(manual_key, None)


__all__ = [
    "validate_slug",
    "branch_name",
    "worktree_path",
    "GitWorktreeIsolator",
    "InMemoryWorktreeRegistry",
]
