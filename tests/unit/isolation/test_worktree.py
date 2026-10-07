"""Real git-worktree tests for GitWorktreeIsolator (docs/reference/subagents.md §3, §9).

Uses the ``tmp_git_repo`` fixture (a real throwaway repo) — the only place real
git is exercised. fail-closed (§3.2) and naming (§3.3) are checked here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoverse.infra.isolation.worktree import (
    GitWorktreeIsolator,
    branch_name,
    validate_slug,
    worktree_path,
)
from autoverse.ports.isolation import FinalizeAction, IsoSpec, IsolationError


def test_validate_slug_rejects_traversal() -> None:
    validate_slug("good-slug_1.2")
    validate_slug("nested/slug")
    for bad in ["", "..", "a/../b", "a b", "x" * 65, "a/" * 200]:
        with pytest.raises(IsolationError):
            validate_slug(bad)


def test_branch_and_path_are_deterministic(tmp_path: Path) -> None:
    assert branch_name("foo") == "autoverse/wt/foo"
    p = worktree_path(tmp_path, "foo")
    assert p == tmp_path / ".autoverse" / "worktrees" / "foo"


async def test_scope_creates_real_worktree_and_cleans_up(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="exp1", lifecycle="turn"), tmp_git_repo) as handle:
        assert handle.cwd.exists()
        assert handle.branch == "autoverse/wt/exp1"
        # writing inside the worktree must NOT touch the main working dir
        (handle.cwd / "scratch.txt").write_text("inside", encoding="utf-8")
        assert not (tmp_git_repo / "scratch.txt").exists()
    # turn lifecycle removes the worktree on exit
    assert not (tmp_git_repo / ".autoverse" / "worktrees" / "exp1").exists()


async def test_scope_inside_nested_git_repo_uses_child_repo_root(
    tmp_git_repo: Path,
) -> None:
    import subprocess

    child = tmp_git_repo / "child"
    child.mkdir()

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git(child, "init", "-q")
    git(child, "config", "user.email", "autoverse@example.test")
    git(child, "config", "user.name", "AutoVerse Tests")
    (child / "README.md").write_text("child\n", encoding="utf-8")
    git(child, "add", "README.md")
    git(child, "commit", "-q", "-m", "init child")

    iso = GitWorktreeIsolator()
    assert await iso.repo_root(child) == child
    async with iso.scope(IsoSpec(slug="nested", lifecycle="manual"), child) as handle:
        assert handle.source_root == child
        assert handle.cwd == child / ".autoverse" / "worktrees" / "nested"
        assert handle.cwd.exists()
        await iso.finalize(handle, FinalizeAction.DISCARD)

    assert not (child / ".autoverse" / "worktrees" / "nested").exists()


async def test_scope_fail_closed_on_non_git_dir(tmp_path: Path) -> None:
    iso = GitWorktreeIsolator()
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    with pytest.raises(IsolationError):
        async with iso.scope(IsoSpec(slug="x"), plain):
            pass  # pragma: no cover — must not be reached


async def test_audit_detects_tracked_change(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="aud", lifecycle="manual"), tmp_git_repo) as handle:
        # modify a tracked file
        (handle.cwd / "README.md").write_text("changed\n", encoding="utf-8")
        tainted, note = await iso.audit(handle, "git_diff_must_be_empty")
        assert tainted is True
        assert note is not None and "VERIFICATION_TAINTED" in note
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_audit_tolerates_untracked_byproduct(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="aud2", lifecycle="manual"), tmp_git_repo) as handle:
        # untracked byproduct only
        (handle.cwd / "build.log").write_text("noise\n", encoding="utf-8")
        tainted, note = await iso.audit(handle, "git_diff_must_be_empty")
        assert tainted is False
        assert note is None
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_has_changes_false_for_pristine_box(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="hc0", lifecycle="manual"), tmp_git_repo) as handle:
        # nothing done in the box → no work to lose
        assert await iso.has_changes(handle) is False
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_has_changes_true_for_tracked_change(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="hc1", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "README.md").write_text("changed\n", encoding="utf-8")
        assert await iso.has_changes(handle) is True
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_has_changes_true_for_untracked_file(tmp_git_repo: Path) -> None:
    # Unlike audit (which tolerates untracked byproducts), has_changes must treat
    # an untracked file as real work — discarding it silently would lose it (P5).
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="hc2", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "draft.txt").write_text("new work\n", encoding="utf-8")
        assert await iso.has_changes(handle) is True
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_has_changes_true_for_new_commit(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="hc3", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "f.py").write_text("x = 1\n", encoding="utf-8")
        await iso.finalize(handle, FinalizeAction.AUTOCOMMIT)
        # committed → clean working tree, but HEAD moved past base_commit
        assert await iso.has_changes(handle) is True
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_has_uncommitted_changes_ignores_new_commit(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="hcu", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "f.py").write_text("x = 1\n", encoding="utf-8")
        await iso.finalize(handle, FinalizeAction.AUTOCOMMIT)
        assert await iso.has_changes(handle) is True
        assert await iso.has_uncommitted_changes(handle) is False
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def _branches(cwd: Path) -> list[str]:
    import subprocess

    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


async def test_discard_removes_branch_not_just_worktree(tmp_git_repo: Path) -> None:
    # Regression: discarding a (transient/empty) box must drop the
    # ``autoverse/wt/<slug>`` branch too, else branches accumulate forever.
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="disc1", lifecycle="manual"), tmp_git_repo) as handle:
        branch = handle.branch
    assert branch in await _branches(tmp_git_repo)
    await iso.finalize(handle, FinalizeAction.DISCARD)
    assert not (tmp_git_repo / ".autoverse" / "worktrees" / "disc1").exists()
    assert branch not in await _branches(tmp_git_repo)  # branch gone too


async def test_discard_force_deletes_committed_branch(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="disc2", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "x.py").write_text("1\n", encoding="utf-8")
        await iso.finalize(handle, FinalizeAction.AUTOCOMMIT)
        branch = handle.branch
    await iso.finalize(handle, FinalizeAction.DISCARD)
    assert branch not in await _branches(tmp_git_repo)  # -D drops unmerged commits


async def test_turn_cleanup_keeps_branch_for_merge(tmp_git_repo: Path) -> None:
    # L1 sub-agent: AUTOCOMMIT then turn-level __aexit__ cleanup must remove the
    # worktree dir but KEEP the branch (it carries commits the parent merges).
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="turn1", lifecycle="turn"), tmp_git_repo) as handle:
        (handle.cwd / "x.py").write_text("1\n", encoding="utf-8")
        await iso.finalize(handle, FinalizeAction.AUTOCOMMIT)
        branch = handle.branch
    assert not (tmp_git_repo / ".autoverse" / "worktrees" / "turn1").exists()
    assert branch in await _branches(tmp_git_repo)  # branch survives for merge


async def test_finalize_keep_does_not_commit(tmp_git_repo: Path) -> None:
    # ports/isolation.py KEEP = "keep worktree + branch, no commit". The worktree
    # must keep uncommitted changes; no new commit may be created (§7.1).
    import subprocess

    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="keep1", lifecycle="manual"), tmp_git_repo) as handle:
        def _count() -> int:
            out = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=handle.cwd, check=True, capture_output=True, text=True,
            ).stdout.strip()
            return int(out)

        before = _count()
        (handle.cwd / "new.py").write_text("x = 1\n", encoding="utf-8")
        result = await iso.finalize(handle, FinalizeAction.KEEP)
        assert result.commit is None
        assert result.removed is False
        assert _count() == before  # no new commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=handle.cwd, check=True, capture_output=True, text=True,
        ).stdout
        assert "new.py" in status  # uncommitted change still present
        await iso.finalize(handle, FinalizeAction.DISCARD)


async def test_finalize_autocommit_returns_commit(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="ac", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "new.py").write_text("x = 1\n", encoding="utf-8")
        result = await iso.finalize(handle, FinalizeAction.AUTOCOMMIT)
        assert result.commit
        assert result.branch == "autoverse/wt/ac"
        await iso.finalize(handle, FinalizeAction.DISCARD)


# ── P3: base_commit always populated from the created worktree's HEAD ──────────
async def test_scope_populates_base_commit(tmp_git_repo: Path) -> None:
    import subprocess

    iso = GitWorktreeIsolator()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_git_repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    async with iso.scope(IsoSpec(slug="bc", lifecycle="manual"), tmp_git_repo) as handle:
        assert handle.base_commit == head  # never None; equals the branch point
        await iso.finalize(handle, FinalizeAction.DISCARD)


# ── P0: real merge-back ───────────────────────────────────────────────────────
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


async def test_finalize_merge_applies_branch_to_base(tmp_git_repo: Path) -> None:
    # The headline P0 fix: 'merge' must actually land the box's work on the base
    # branch, then remove the worktree + branch.
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="mg1", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "feature.py").write_text("FEATURE\n", encoding="utf-8")
        branch = handle.branch
        result = await iso.finalize(handle, FinalizeAction.MERGE)

    assert result.removed is True
    assert result.note == "merged"
    assert result.commit  # the merge commit on base
    # the change is now in the MAIN work-tree (this is what "merge back" means)
    assert (tmp_git_repo / "feature.py").exists()
    assert _read(tmp_git_repo / "feature.py") == "FEATURE\n"
    # box + branch are gone
    assert not (tmp_git_repo / ".autoverse" / "worktrees" / "mg1").exists()
    assert branch not in await _branches(tmp_git_repo)


async def test_finalize_merge_nothing_to_merge_removes_empty_box(tmp_git_repo: Path) -> None:
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="mg2", lifecycle="manual"), tmp_git_repo) as handle:
        branch = handle.branch
        result = await iso.finalize(handle, FinalizeAction.MERGE)  # pristine box

    assert result.removed is True
    assert "nothing to merge" in result.note
    assert not (tmp_git_repo / ".autoverse" / "worktrees" / "mg2").exists()
    assert branch not in await _branches(tmp_git_repo)


async def test_finalize_merge_conflict_is_failsafe(tmp_git_repo: Path) -> None:
    # Divergent edits to the same line → merge conflict. MERGE must abort, leave
    # the base work-tree clean, and KEEP the branch + worktree for a manual merge.
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_git_repo, check=True, capture_output=True, text=True,
        ).stdout

    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="mg3", lifecycle="manual"), tmp_git_repo) as handle:
        # box edits README line 1
        (handle.cwd / "README.md").write_text("from-box\n", encoding="utf-8")
        branch = handle.branch
        # base diverges on the SAME line, committed (so base is clean)
        (tmp_git_repo / "README.md").write_text("from-base\n", encoding="utf-8")
        git("commit", "-aqm", "base diverges")

        result = await iso.finalize(handle, FinalizeAction.MERGE)

    assert result.removed is False  # fail-safe: box kept
    assert "conflict" in result.note
    assert branch in await _branches(tmp_git_repo)  # branch survives for manual merge
    assert (tmp_git_repo / ".autoverse" / "worktrees" / "mg3").exists()
    # base work-tree must be restored by merge --abort: no unmerged entries, no
    # merge in progress, README back to base content (only our own .autoverse/
    # infra dir remains untracked).
    assert not any(ln[:2] in ("UU", "AA", "DD") for ln in git("status", "--porcelain").splitlines())
    assert not (tmp_git_repo / ".git" / "MERGE_HEAD").exists()
    assert _read(tmp_git_repo / "README.md") == "from-base\n"
    await iso.finalize(handle, FinalizeAction.DISCARD)  # cleanup


async def test_finalize_merge_skips_dirty_base(tmp_git_repo: Path) -> None:
    # Never merge onto a dirty base work-tree — keep the branch instead.
    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="mg4", lifecycle="manual"), tmp_git_repo) as handle:
        (handle.cwd / "feature.py").write_text("FEATURE\n", encoding="utf-8")
        branch = handle.branch
        # dirty the base work-tree with an uncommitted change
        (tmp_git_repo / "README.md").write_text("dirty\n", encoding="utf-8")

        result = await iso.finalize(handle, FinalizeAction.MERGE)

    assert result.removed is False
    assert "not clean" in result.note
    assert branch in await _branches(tmp_git_repo)
    assert not (tmp_git_repo / "feature.py").exists()  # nothing merged in
    await iso.finalize(handle, FinalizeAction.DISCARD)  # cleanup


# ── P2: prune is best-effort + non-destructive ────────────────────────────────
async def test_prune_drops_stale_registration(tmp_git_repo: Path) -> None:
    import shutil
    import subprocess

    iso = GitWorktreeIsolator()
    async with iso.scope(IsoSpec(slug="pr1", lifecycle="manual"), tmp_git_repo) as handle:
        wt_dir = handle.cwd
    # Simulate a crash: the worktree dir vanishes but its registration lingers.
    shutil.rmtree(wt_dir)
    listed_before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_git_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "pr1" in listed_before  # stale entry present
    assert await iso.prune(tmp_git_repo) == 1
    listed_after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_git_repo, check=True, capture_output=True, text=True,
    ).stdout
    assert "pr1" not in listed_after  # stale entry pruned
    await iso.finalize(handle, FinalizeAction.DISCARD)  # best-effort branch cleanup


# ── P1: concurrent same-repo creation is serialised (no index/ref race) ───────
async def test_concurrent_creation_on_same_repo(tmp_git_repo: Path) -> None:
    import asyncio

    # Two independent isolators (as two sessions would have) build worktrees on
    # the SAME repo at once. The shared per-repo lock must let both succeed.
    iso_a = GitWorktreeIsolator()
    iso_b = GitWorktreeIsolator()

    async def build(iso: GitWorktreeIsolator, slug: str) -> str:
        async with iso.scope(IsoSpec(slug=slug, lifecycle="manual"), tmp_git_repo) as h:
            assert h.cwd.exists()
            return h.branch or ""

    branches = await asyncio.gather(build(iso_a, "cc1"), build(iso_b, "cc2"))
    live = await _branches(tmp_git_repo)
    assert "autoverse/wt/cc1" in live
    assert "autoverse/wt/cc2" in live
    assert set(branches) == {"autoverse/wt/cc1", "autoverse/wt/cc2"}
