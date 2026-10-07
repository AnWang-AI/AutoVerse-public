"""GitShadowSnapshot — a SnapshotPort backed by a shadow git directory.

The shadow repo keeps its objects/index in a *separate* ``GIT_DIR`` while its
work-tree is the live ``cwd``. This lets us snapshot/restore the workspace
without creating commits in (or otherwise touching) the user's own git history
(docs/reference/tools.md §9.4):

* :meth:`track` stages everything (``git add -A`` — honours ``.gitignore``) and
  writes a tree object. The tree hash *is* the snapshot id. If nothing changed
  since the last tracked tree, it returns ``None`` (no redundant snapshot).
* :meth:`diff` shows the unified diff between a tracked tree and the current
  work-tree.
* :meth:`restore` resets the index + work-tree to a tracked tree (``read-tree``
  then ``checkout-index -fa``) and reports the paths that changed.

Capture and diff remain best-effort for ordinary turn execution. Required WebUI
turn-boundary capture and restore raise on failure so the caller can fail closed:
conversation history must never be truncated when files were not restored. All
subprocesses run via ``asyncio`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from autoverse.ports.snapshot import SnapshotRef


class GitShadowSnapshot:
    """A :class:`~autoverse.ports.snapshot.SnapshotPort` over a shadow git dir."""

    def __init__(
        self,
        work_tree: Path | str,
        git_dir: Path | str,
        *,
        large_file_limit: int = 5 * 1024 * 1024,
    ) -> None:
        self._work = Path(work_tree)
        self._git = Path(git_dir)
        #: Files larger than this (bytes) are excluded from the snapshot so a
        #: stray blob/artifact never bloats the shadow objects. ``0`` disables.
        self._large_file_limit = large_file_limit
        self._initialised = False
        self._last_tree: str | None = None

    @property
    def work_tree(self) -> Path:
        """The workspace root captured by this adapter."""
        return self._work

    # ── SnapshotPort ─────────────────────────────────────────────────────────
    async def track(self) -> SnapshotRef | None:
        if not await self._ensure_repo():
            return None
        if await self._git_run("add", "-A") is None:
            return None
        await self._unstage_oversized()
        listing = await self._git_run("ls-files")
        names = tuple(f for f in (listing or "").splitlines() if f)
        tree = await self._git_run("write-tree")
        if tree is None:
            return None
        tree = tree.strip()
        if not tree or tree == self._last_tree:
            return None  # nothing new to track (P1: no redundant snapshot)
        self._last_tree = tree
        return SnapshotRef(
            hash=tree,
            created_at=datetime.now(timezone.utc).isoformat(),
            files=names,
        )

    async def track_rewind(self) -> SnapshotRef:
        """Capture an exact turn boundary, returning the tree even when unchanged."""
        if not await self._ensure_repo():
            raise RuntimeError("could not initialize workspace snapshot")
        if await self._git_run("add", "-A") is None:
            raise RuntimeError("could not capture workspace snapshot")
        await self._unstage_oversized()
        listing = await self._git_run("ls-files")
        if listing is None:
            raise RuntimeError("could not list workspace snapshot files")
        tree = await self._git_run("write-tree")
        if tree is None or not tree.strip():
            raise RuntimeError("could not write workspace snapshot")
        snapshot_hash = tree.strip()
        self._last_tree = snapshot_hash
        return SnapshotRef(
            hash=snapshot_hash,
            created_at=datetime.now(timezone.utc).isoformat(),
            files=tuple(name for name in listing.splitlines() if name),
        )

    async def _unstage_oversized(self) -> None:
        """Drop files over ``large_file_limit`` from the index before write-tree."""
        if self._large_file_limit <= 0:
            return
        listing = await self._git_run("ls-files")
        big: list[str] = []
        for name in (listing or "").splitlines():
            if not name:
                continue
            path = self._work / name
            try:
                if path.is_file() and path.stat().st_size > self._large_file_limit:
                    big.append(name)
            except OSError:
                continue
        if big:
            await self._git_run("rm", "--cached", "-q", "--", *big)

    async def diff(self, ref: SnapshotRef) -> str:
        if not await self._ensure_repo():
            return ""
        # Stage first so untracked/changed files are reflected, then diff the
        # tracked tree against the index (cached) — a stable view of the delta.
        await self._git_run("add", "-A")
        out = await self._git_run("diff", "--cached", ref.hash)
        return out or ""

    async def restore(self, ref: SnapshotRef) -> tuple[str, ...]:
        if not await self._ensure_repo():
            raise RuntimeError("could not initialize workspace snapshot")
        if await self._git_run("add", "-A") is None:
            raise RuntimeError("could not inspect current workspace state")
        names_out = await self._git_run("diff", "--cached", "--name-only", ref.hash)
        if names_out is None:
            raise RuntimeError("workspace snapshot is no longer available")
        changed = tuple(f for f in (names_out or "").splitlines() if f)
        # ``--reset -u`` updates the work-tree as well as the shadow index. This
        # also removes files created after the snapshot; checkout-index alone
        # only overwrote retained paths and leaked newly-created files.
        read = await self._git_run("read-tree", "--reset", "-u", ref.hash)
        if read is None:
            raise RuntimeError("could not restore workspace snapshot")
        self._last_tree = ref.hash
        return changed

    async def resolve(self, snapshot_hash: str) -> SnapshotRef | None:
        """Rehydrate a persisted tree reference after a process restart."""
        if not snapshot_hash or not await self._ensure_repo():
            return None
        exists = await self._git_run("cat-file", "-e", f"{snapshot_hash}^{{tree}}")
        if exists is None:
            return None
        return SnapshotRef(hash=snapshot_hash, created_at="", files=())

    # ── internals ────────────────────────────────────────────────────────────
    async def _ensure_repo(self) -> bool:
        if self._initialised:
            return True
        if not self._work.is_dir():
            return False
        try:
            self._git.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        if not (self._git / "HEAD").exists():
            init = await self._git_run("init", "-q")
            if init is None:
                return False
        self._initialised = True
        return True

    async def _git_run(self, *args: str) -> str | None:
        """Run a shadow-git command; return stdout text, or ``None`` on failure."""
        cmd = (
            "git",
            f"--git-dir={self._git}",
            f"--work-tree={self._work}",
            *args,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._work),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _err = await proc.communicate()
        except (OSError, ValueError):  # git missing / bad args
            return None
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", errors="replace")


__all__ = ["GitShadowSnapshot"]
