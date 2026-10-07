"""worktree-mode orchestration unit tests (docs/reference/subagents.md §5a, docs/guides/using-cli-and-tui.md §3.10).

Drives ``core.loop.worktree_mode`` against the FakeIsolator double + a minimal
AppContext: enter switches cwd and sets the flag; fail-closed enter leaves the
session untouched; plan/worktree are mutually exclusive; exit finalizes and
restores cwd.
"""

from __future__ import annotations

from pathlib import Path

from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.loop import worktree_mode
from autoverse.core.tools.builtin.worktree import (
    ExitWorktreeModeArgs,
    ExitWorktreeModeTool,
)
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.infra.isolation.worktree import InMemoryWorktreeRegistry
from autoverse.types import AbortController, Decision
from autoverse.types.tools import ToolCall

from tests._doubles import FakeIsolator


class _AllowAll:
    async def check(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.allow()

    async def request(self, call: ToolCall) -> Decision:  # pragma: no cover
        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class _Sink:
    async def emit(self, event: object) -> None:  # pragma: no cover - unused
        ...


def _app(cwd: Path, *, isolator: object | None) -> AppContext:
    app = AppContext(
        config=Config(),
        store=None,  # type: ignore[arg-type]
        cwd=cwd,
        model="m",
        events=_Sink(),
    )
    app.isolator = isolator  # type: ignore[assignment]
    app.worktrees = InMemoryWorktreeRegistry()
    app.extra["session_id"] = "s1"
    return app


def _tool_ctx(app: AppContext) -> ToolContext:
    return ToolContext(
        cwd=app.cwd,
        permissions=_AllowAll(),
        tracker=FileTracker(),
        signal=AbortController().signal,
        events=_Sink(),
        app=app,
    )


async def test_enter_switches_cwd_and_sets_flag(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    app = _app(tmp_path, isolator=FakeIsolator(cwd=wt))

    res = await worktree_mode.enter(app, reason="r")

    assert res.ok and res.branch == "autoverse/wt/fake"
    assert worktree_mode.is_active(app) is True
    assert app.cwd == wt  # session cwd switched into the worktree
    assert app.extra["worktree_mode"]["branch"] == "autoverse/wt/fake"
    assert app.extra["worktree_mode"]["source_root"] == str(tmp_path)
    assert app.extra["group_root"] == str(tmp_path)
    # registered for the session (L2/manual)
    assert app.worktrees.active_manual("s1") is not None


async def test_enter_from_repo_subdir_preserves_relative_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subdir = repo / "pkg"
    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    subdir.mkdir(parents=True)
    app = _app(subdir, isolator=FakeIsolator(cwd=wt, source_root=repo))

    res = await worktree_mode.enter(app, reason="r")

    assert res.ok and res.cwd == wt / "pkg"
    assert app.cwd == wt / "pkg"
    assert app.extra["worktree_mode"]["worktree_root"] == str(wt)
    assert app.extra["worktree_mode"]["worktree_cwd"] == str(wt / "pkg")

    out = await worktree_mode.exit(app, action="discard")
    assert out.ok
    assert app.cwd == subdir


async def test_turn_loop_uses_worktree_root_not_subdir_for_isolation(
    tmp_path: Path,
) -> None:
    from autoverse.core.loop.turn_loop import TurnLoop

    repo = tmp_path / "repo"
    subdir = repo / "pkg"
    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    subdir.mkdir(parents=True)
    app = _app(subdir, isolator=FakeIsolator(cwd=wt, source_root=repo))
    await worktree_mode.enter(app)
    assert app.cwd == wt / "pkg"

    loop = object.__new__(TurnLoop)
    loop._app = app  # type: ignore[attr-defined]
    root, source = loop._isolation_paths()  # type: ignore[attr-defined]

    assert root == wt
    assert source == repo


async def test_enter_path_targets_nested_repo_and_restores_original_cwd(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child_wt = tmp_path / "child-wt"
    parent.mkdir()
    child.mkdir()
    child_wt.mkdir()
    app = _app(parent, isolator=FakeIsolator(cwd=child_wt, source_root=child))

    res = await worktree_mode.enter(app, base_path="child")

    assert res.ok
    assert app.cwd == child_wt
    assert app.extra["worktree_mode"]["base_cwd"] == str(child.resolve(strict=False))
    assert app.extra["worktree_mode"]["restore_cwd"] == str(parent)

    out = await worktree_mode.exit(app, action="discard")
    assert out.ok
    assert app.cwd == parent


async def test_enter_fail_closed_leaves_session_untouched(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=FakeIsolator(scope_error=True))

    res = await worktree_mode.enter(app)

    assert res.ok is False and res.error
    assert worktree_mode.is_active(app) is False
    assert app.cwd == tmp_path  # cwd NOT switched on failure (no silent degrade)


async def test_enter_without_isolator_fails(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=None)
    res = await worktree_mode.enter(app)
    assert res.ok is False
    assert worktree_mode.is_active(app) is False


async def test_enter_clears_plan_mode_mutual_exclusion(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=FakeIsolator(cwd=tmp_path / "wt"))
    app.extra["plan_mode"] = {"active": True, "reason": "plan"}

    await worktree_mode.enter(app)

    assert app.extra["plan_mode"]["active"] is False  # plan cleared
    assert worktree_mode.is_active(app) is True


async def test_exit_finalizes_and_restores_cwd(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    assert app.cwd == wt

    res = await worktree_mode.exit(app, action="keep")

    assert res.ok and res.action == "keep"
    assert "finalize" in iso.calls
    assert worktree_mode.is_active(app) is False
    assert app.cwd == tmp_path  # restored to the original work-tree


async def test_exit_when_not_active_is_noop(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=FakeIsolator())
    res = await worktree_mode.exit(app, action="keep")
    assert res.ok is False and res.error


async def test_double_enter_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=FakeIsolator(cwd=tmp_path / "wt"))
    assert (await worktree_mode.enter(app)).ok is True
    second = await worktree_mode.enter(app)
    assert second.ok is False and "already" in (second.error or "")


async def test_has_changes_reports_isolator_verdict(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    app = _app(tmp_path, isolator=FakeIsolator(cwd=wt, dirty=True))
    await worktree_mode.enter(app)

    assert await worktree_mode.has_changes(app) is True


async def test_has_changes_false_for_pristine_box(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    app = _app(tmp_path, isolator=FakeIsolator(cwd=wt, dirty=False))
    await worktree_mode.enter(app)

    # A freshly-created box with no work reports False → REPL skips the dialog.
    assert await worktree_mode.has_changes(app) is False


async def test_has_changes_none_when_not_active(tmp_path: Path) -> None:
    app = _app(tmp_path, isolator=FakeIsolator(cwd=tmp_path / "wt"))
    # Not in worktree mode → cannot determine → conservative None.
    assert await worktree_mode.has_changes(app) is None


async def test_exit_merge_maps_to_merge_and_cleans_registry(tmp_path: Path) -> None:
    # 'merge' must reach FinalizeAction.MERGE (not AUTOCOMMIT). The fake reports
    # removed=True for MERGE, so the registry entry is dropped and the note flows
    # through to the caller.
    from autoverse.ports.isolation import FinalizeAction

    wt = tmp_path / "wt"
    iso = FakeIsolator(cwd=wt)
    seen: dict[str, object] = {}
    orig_finalize = iso.finalize

    async def _spy(handle: object, action: object) -> object:
        seen["action"] = action
        return await orig_finalize(handle, action)  # type: ignore[arg-type]

    iso.finalize = _spy  # type: ignore[assignment,method-assign]
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    assert app.worktrees.active_manual("s1") is not None

    res = await worktree_mode.exit(app, action="merge")

    assert seen["action"] is FinalizeAction.MERGE  # not AUTOCOMMIT (the old bug)
    assert res.ok and res.action == "merge"
    assert res.removed is True and res.note == "merged"
    assert app.worktrees.active_manual("s1") is None  # registry cleaned on removal


async def test_exit_keep_retains_registry_entry(tmp_path: Path) -> None:
    # KEEP does not remove the box → the registry entry stays (still tracked).
    wt = tmp_path / "wt"
    app = _app(tmp_path, isolator=FakeIsolator(cwd=wt))
    await worktree_mode.enter(app)
    res = await worktree_mode.exit(app, action="keep")
    assert res.removed is False
    assert app.worktrees.active_manual("s1") is not None


async def test_exit_tool_without_action_requires_choice_for_dirty_worktree(
    tmp_path: Path,
) -> None:
    wt = tmp_path / "wt"
    iso = FakeIsolator(cwd=wt, dirty=True)
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    iso.calls.clear()

    result = await ExitWorktreeModeTool().run(
        ExitWorktreeModeArgs(), _tool_ctx(app)
    )

    assert result.is_error
    assert result.error_kind == "worktree_exit_action_required"
    assert result.metadata["needs_action"] is True
    assert worktree_mode.is_active(app) is True
    assert app.cwd == wt
    assert "has_changes" in iso.calls
    assert "finalize" not in iso.calls


async def test_exit_tool_without_action_discards_pristine_worktree(
    tmp_path: Path,
) -> None:
    wt = tmp_path / "wt"
    iso = FakeIsolator(cwd=wt, dirty=False)
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    assert app.worktrees.active_manual("s1") is not None
    iso.calls.clear()

    result = await ExitWorktreeModeTool().run(
        ExitWorktreeModeArgs(), _tool_ctx(app)
    )

    assert not result.is_error
    assert result.metadata["action"] == "discard"
    assert result.metadata["removed"] is True
    assert worktree_mode.is_active(app) is False
    assert app.cwd == tmp_path
    assert app.worktrees.active_manual("s1") is None
    assert iso.calls == ["has_changes", "finalize"]


async def test_enter_calls_prune_best_effort(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    assert "prune" in iso.calls  # stale-registration sweep ran on entry


async def test_enter_records_orphan_worktree_dirs(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    orphan = tmp_path / ".autoverse" / "worktrees" / "orphan"
    orphan.mkdir(parents=True)
    app = _app(tmp_path, isolator=FakeIsolator(cwd=wt))

    res = await worktree_mode.enter(app)

    assert res.orphan_count == 1
    assert app.extra["worktree_orphans"] == [str(orphan)]


async def test_reenter_reuses_existing_session_worktree(tmp_path: Path) -> None:
    # Sticky reuse: Tab out (keep) then Tab back must resume the SAME box, not
    # mint a fresh throwaway one. Otherwise every Tab orphans a worktree.
    wt = tmp_path / "wt"
    wt.mkdir()  # reuse only triggers if the box dir still exists
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)

    r1 = await worktree_mode.enter(app)
    assert r1.ok and r1.reused is False
    await worktree_mode.exit(app, action="keep")  # keep retains the registry entry
    assert app.worktrees.active_manual("s1") is not None

    iso.calls.clear()
    r2 = await worktree_mode.enter(app)

    assert r2.ok and r2.reused is True
    assert r2.has_changes is False
    assert r2.cwd == wt
    assert app.cwd == wt and worktree_mode.is_active(app)
    assert "scope" not in iso.calls  # no new box was built


async def test_reenter_different_repo_builds_new_worktree(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    parent_wt = tmp_path / "parent-wt"
    child_wt = tmp_path / "child-wt"
    parent.mkdir()
    child.mkdir()
    parent_wt.mkdir()
    child_wt.mkdir()
    iso = FakeIsolator(cwd=parent_wt, source_root=parent)
    app = _app(parent, isolator=iso)

    first = await worktree_mode.enter(app)
    assert first.ok and first.reused is False
    await worktree_mode.exit(app, action="keep")
    assert app.worktrees.active_manual("s1", parent) is not None

    app.cwd = child
    iso._cwd = child_wt
    iso._source_root = child
    iso.calls.clear()
    second = await worktree_mode.enter(app)

    assert second.ok and second.reused is False
    assert app.cwd == child_wt
    assert "scope" in iso.calls
    assert app.worktrees.active_manual("s1", parent) is not None
    assert app.worktrees.active_manual("s1", child) is not None


async def test_reenter_can_force_new_instead_of_reuse(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)

    await worktree_mode.enter(app)
    await worktree_mode.exit(app, action="keep")

    iso.calls.clear()
    r2 = await worktree_mode.enter(app, reuse=False)

    assert r2.ok and r2.reused is False
    assert "scope" in iso.calls


async def test_reenter_reports_existing_dirty_state(tmp_path: Path) -> None:
    # A worktree kept from a prior session (registry entry preserved) whose
    # directory still has changes.  This can happen when the REPL user exits
    # via Tab without committing, or when a commit was made past base_commit.
    wt = tmp_path / "wt"
    wt.mkdir()
    iso = FakeIsolator(cwd=wt, dirty=True)
    app = _app(tmp_path, isolator=iso)

    # Enter → the worktree is live and dirty.
    await worktree_mode.enter(app)

    # Simulate the exit-through-UI path: the registry entry stays, but the
    # worktree-mode flag is cleared (the REPL's _switch_mode handles this
    # without calling the low-level exit() which now rejects dirty state).
    app.extra[worktree_mode.WORKTREE_MODE_KEY] = {"active": False}
    assert app.worktrees.active_manual("s1") is not None

    r2 = await worktree_mode.enter(app)

    assert r2.reused is True
    assert r2.has_changes is True


async def test_reenter_after_discard_builds_new(tmp_path: Path) -> None:
    # A real finalize (discard) clears the registry → next enter is a fresh box.
    wt = tmp_path / "wt"
    wt.mkdir()
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)

    await worktree_mode.enter(app)
    await worktree_mode.exit(app, action="discard")  # removed=True → registry cleaned
    assert app.worktrees.active_manual("s1") is None

    iso.calls.clear()
    r2 = await worktree_mode.enter(app)

    assert r2.ok and r2.reused is False
    assert "scope" in iso.calls  # built a fresh box


async def test_exit_keep_rejects_uncommitted_changes(tmp_path: Path) -> None:
    """exit(action='keep') refuses when the worktree has uncommitted changes —
    the agent must commit first, then retry."""
    wt = tmp_path / "wt"
    wt.mkdir()
    iso = FakeIsolator(cwd=wt, dirty=True)
    app = _app(tmp_path, isolator=iso)

    await worktree_mode.enter(app)
    iso.calls.clear()
    res = await worktree_mode.exit(app, action="keep")

    assert res.ok is False
    assert "Uncommitted changes" in (res.error or "")
    assert iso.calls == ["has_uncommitted_changes"]
    # Must still be in worktree mode — agent can commit and retry.
    assert worktree_mode.is_active(app)
    assert app.cwd == wt

    # After committing (simulated by clearing dirty), retry succeeds.
    iso._dirty = False
    iso.calls.clear()
    res2 = await worktree_mode.exit(app, action="keep")
    assert res2.ok is True
    assert iso.calls == ["has_uncommitted_changes", "finalize"]

async def test_reenter_stale_registration_builds_new(tmp_path: Path) -> None:
    # If the registered box dir vanished out-of-band, drop the stale entry and
    # build a fresh one rather than resuming a dead path.
    wt = tmp_path / "wt"
    wt.mkdir()
    iso = FakeIsolator(cwd=wt)
    app = _app(tmp_path, isolator=iso)
    await worktree_mode.enter(app)
    await worktree_mode.exit(app, action="keep")
    wt.rmdir()  # simulate the worktree dir disappearing

    iso.calls.clear()
    r2 = await worktree_mode.enter(app)

    assert r2.ok and r2.reused is False
    assert "scope" in iso.calls
