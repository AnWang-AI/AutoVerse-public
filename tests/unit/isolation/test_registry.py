"""InMemoryWorktreeRegistry: explicit per-session state, no singleton (§3.4, P4)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
from autoverse.ports.isolation import IsoHandle


def test_session_and_manual_are_separate_keys() -> None:
    reg = InMemoryWorktreeRegistry()
    sess = IsoHandle(cwd=Path("/wt/sess"), branch="b1")
    manual = IsoHandle(cwd=Path("/wt/man"), branch="b2")
    reg.register_session("s1", sess)
    reg.register_manual("s1", manual)
    assert reg.active_for_session("s1") is sess
    assert reg.active_manual("s1") is manual
    assert reg.active_for_session("other") is None


def test_manual_worktrees_are_keyed_by_repo_root() -> None:
    reg = InMemoryWorktreeRegistry()
    parent = IsoHandle(
        cwd=Path("/repo/.autoverse/worktrees/parent"),
        branch="b1",
        source_root=Path("/repo"),
    )
    child = IsoHandle(
        cwd=Path("/repo/child/.autoverse/worktrees/child"),
        branch="b2",
        source_root=Path("/repo/child"),
    )

    reg.register_manual("s1", parent)
    reg.register_manual("s1", child)

    assert reg.active_manual("s1", Path("/repo")) is parent
    assert reg.active_manual("s1", Path("/repo/child")) is child
    assert reg.active_manual("s1") is None  # ambiguous without a repo root


def test_two_instances_do_not_share_state() -> None:
    a = InMemoryWorktreeRegistry()
    b = InMemoryWorktreeRegistry()
    a.register_session("s", IsoHandle(cwd=Path("/x"), branch="b"))
    assert b.active_for_session("s") is None


async def test_cleanup_removes_handle() -> None:
    reg = InMemoryWorktreeRegistry()
    h = IsoHandle(cwd=Path("/wt/sess"), branch="b1")
    reg.register_session("s1", h)
    await reg.cleanup(h)
    assert reg.active_for_session("s1") is None


def test_list_orphans_finds_unknown_dirs(tmp_path: Path) -> None:
    reg = InMemoryWorktreeRegistry()
    wt_root = tmp_path / ".autoverse" / "worktrees"
    (wt_root / "orphan").mkdir(parents=True)
    known = IsoHandle(cwd=wt_root / "known", branch="b")
    (wt_root / "known").mkdir()
    reg.register(known, "session")
    orphans = reg.list_orphans(tmp_path)
    assert (wt_root / "orphan") in orphans
    assert (wt_root / "known") not in orphans
