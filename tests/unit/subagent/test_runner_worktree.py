"""Regression tests for sub-agent worktree targeting and metadata."""

from __future__ import annotations

from pathlib import Path

from autoverse.core.subagent.jobs import InMemoryJobStore, ResourceLock
from autoverse.core.subagent.runner import SpawnRequest, SubAgentRunner

from tests._doubles import FakeEngineFactory, FakeIsolator
from tests.conftest import RecordingSink, make_ctx


async def test_worktree_isolation_uses_target_cwd_and_reports_metadata(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    target = tmp_path / "target"
    wt = tmp_path / "target-wt"
    parent.mkdir()
    target.mkdir()
    wt.mkdir()
    sink = RecordingSink()
    ctx = make_ctx(parent, sink=sink)
    isolator = FakeIsolator(cwd=wt)
    factory = FakeEngineFactory()
    runner = SubAgentRunner(
        engine_factory=factory,
        isolator=isolator,
        resource_lock=ResourceLock(),
        job_store=InMemoryJobStore(),
        events=sink,
    )

    result = await runner.spawn(
        SpawnRequest(
            subagent_type="builder",
            task="edit target",
            isolation="worktree",
            target_cwd=str(target),
        ),
        ctx,
    )

    assert isolator.base_cwds == [target]
    assert factory.constructions[-1]["cwd"] == wt
    assert factory.constructions[-1]["isolation_root"] == wt
    assert factory.constructions[-1]["isolation_source_root"] == target
    assert result.trace_id
    assert result.worktree_branch == "autoverse/wt/fake"
    assert result.worktree_path == str(wt)
    assert result.worktree_commit == "commit-sha"
