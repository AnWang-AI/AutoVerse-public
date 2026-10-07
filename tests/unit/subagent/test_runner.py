"""SubAgentRunner orchestration unit tests (docs/reference/subagents.md §3.2; S1/S4/S6/S8/S10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import autoverse.core.tools.builtin  # noqa: F401 — trigger registration
from autoverse.app.config import Config
from autoverse.app.context import AppContext
from autoverse.core.subagent.jobs import InMemoryJobStore, ResourceLock
from autoverse.core.subagent.runner import (
    RunOutcome,
    SpawnRequest,
    SubAgentRunner,
)
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import ToolRegistry
from autoverse.core.tools.tracker import FileTracker
from autoverse.ports.isolation import IsolationError
from autoverse.types.abort import AbortController

from tests._doubles import FakeEngineFactory, FakeIsolator


class _Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _Perms:
    async def check(self, c: Any) -> Any: ...
    def check_sync(self, c: Any) -> Any: ...
    async def request_approval(self, c: Any) -> Any: ...
    def cancel_all(self) -> None: ...


def _ctx(cwd: Path, *, spawn: Any = None) -> ToolContext:
    ctrl = AbortController()
    app = AppContext(
        config=Config(),
        store=_DummyStore(),
        cwd=cwd,
        model="parent-model",
        events=_Sink(),
        tools=ToolRegistry.build(),
        spawn=spawn,
    )
    return ToolContext(
        cwd=cwd,
        permissions=_Perms(),
        tracker=FileTracker(),
        signal=ctrl.signal,
        events=app.events,
        app=app,
    )


class _DummyStore:
    async def append(self, *a: Any, **k: Any) -> None: ...
    async def load(self, *a: Any, **k: Any) -> list[Any]:
        return []
    async def rewrite(self, *a: Any, **k: Any) -> None: ...
    async def create(self, *a: Any, **k: Any) -> None: ...
    async def load_meta(self, *a: Any, **k: Any) -> Any: ...
    async def update_meta(self, *a: Any, **k: Any) -> None: ...
    async def list_sessions(self) -> list[str]:
        return []
    async def delete(self, *a: Any, **k: Any) -> None: ...


def _runner(
    *,
    factory: Any = None,
    isolator: Any = None,
    job_store: Any = None,
    lock: Any = None,
) -> SubAgentRunner:
    return SubAgentRunner(
        engine_factory=factory or FakeEngineFactory(RunOutcome(summary="done", verdict="PASS")),
        isolator=isolator or FakeIsolator(),
        resource_lock=lock or ResourceLock(),
        job_store=job_store or InMemoryJobStore(),
        events=_Sink(),
    )


def test_S1_spawn_request_requires_subagent_type() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SpawnRequest(task="do x")  # type: ignore[call-arg]


async def test_S1_spawn_defensive_rejects_empty_type(tmp_path: Path) -> None:
    runner = _runner()
    req = SpawnRequest.model_construct(subagent_type="", task="x")
    with pytest.raises(ValueError):
        await runner.spawn(req, _ctx(tmp_path))


async def test_foreground_returns_summary(tmp_path: Path) -> None:
    runner = _runner()
    res = await runner.spawn(SpawnRequest(subagent_type="scout", task="find X"), _ctx(tmp_path))
    assert res.summary == "done"
    assert res.verdict == "PASS"
    assert res.subagent_type == "scout"


async def test_S4_foreground_and_background_share_one_path(tmp_path: Path) -> None:
    # Background dispatch wraps the SAME _run_one coroutine — assert the engine
    # was constructed identically for fg and bg (one execution body).
    factory = FakeEngineFactory(RunOutcome(summary="ok"))
    runner = _runner(factory=factory)

    spawned: list[Any] = []

    def spawn(coro: Any) -> None:
        spawned.append(coro)

    # foreground
    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), _ctx(tmp_path))
    fg_construction = dict(factory.constructions[-1])

    # background: same task → coroutine launched via app.spawn
    res = await runner.spawn(
        SpawnRequest(subagent_type="scout", task="t", run_in_background=True),
        _ctx(tmp_path, spawn=spawn),
    )
    assert res.trace_id and res.trace_id.startswith("job-")
    assert len(spawned) == 1
    # run the background coroutine to completion
    await spawned[0]
    bg_construction = dict(factory.constructions[-1])

    # Same execution body: the engine is constructed with identical scalar kwargs
    # and the same filtered tool set (registry instances differ by identity only).
    def normalize(c: dict[str, Any]) -> dict[str, Any]:
        # dispatch_id is intentionally unique per spawn (panel keying), so it is
        # excluded from the fg/bg execution-equivalence check.
        out = {k: v for k, v in c.items() if k not in ("cwd", "tools", "dispatch_id")}
        out["tool_names"] = sorted(c["tools"].names()) if c.get("tools") else None
        return out

    assert normalize(fg_construction) == normalize(bg_construction)


async def test_S6_isolation_fail_closed_propagates(tmp_path: Path) -> None:
    runner = _runner(isolator=FakeIsolator(scope_error=True))
    with pytest.raises(IsolationError):
        await runner.spawn(
            SpawnRequest(subagent_type="builder", task="t", isolation="worktree"),
            _ctx(tmp_path),
        )


async def test_S8_audit_before_finalize_taints_verdict(tmp_path: Path) -> None:
    iso = FakeIsolator(audit_tainted=True)
    factory = FakeEngineFactory(RunOutcome(summary="probed", verdict="PASS"))
    runner = _runner(factory=factory, isolator=iso)
    # e2e-verifier forces isolation + post_audit
    res = await runner.spawn(SpawnRequest(subagent_type="e2e-verifier", task="t"), _ctx(tmp_path))
    # audit must run BEFORE finalize
    assert iso.calls.index("audit") < iso.calls.index("finalize")
    assert res.tainted is True
    assert res.verdict is None  # tainted voids the verdict
    assert "VERIFICATION_TAINTED" in res.summary


async def test_S10_max_turns_degrade_nonempty_summary(tmp_path: Path) -> None:
    # engine reports degraded with no textual summary → hard-floor non-empty summary
    factory = FakeEngineFactory(RunOutcome(summary="", degraded="max_turns"))
    runner = _runner(factory=factory)
    res = await runner.spawn(SpawnRequest(subagent_type="builder", task="t"), _ctx(tmp_path))
    assert res.degraded == "max_turns"
    assert res.summary.strip() != ""
    assert "max_turns" in res.summary


async def test_no_isolation_does_not_call_scope(tmp_path: Path) -> None:
    iso = FakeIsolator()
    runner = _runner(isolator=iso)
    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), _ctx(tmp_path))
    assert "scope" not in iso.calls


class _RaisingEngine:
    """A child engine whose run() raises after registering a bound pgid."""

    def __init__(self, **kwargs: Any) -> None:
        self.pgids: tuple[int, ...] = (424242,)

    async def run(self) -> Any:
        raise RuntimeError("engine boom")


class _RaisingFactory:
    def __call__(self, **kwargs: Any) -> _RaisingEngine:
        return _RaisingEngine(**kwargs)


async def test_S9_reap_runs_on_engine_exception(tmp_path: Path, monkeypatch: Any) -> None:
    # §7.4: bound process groups must be reclaimed on the exception path too.
    # The engine raises after a pgid was registered → reap must still fire.
    from autoverse.core.subagent import runner as runner_mod

    reaped: list[tuple[int, ...]] = []

    def _fake_reap(pgids: Any) -> list[int]:
        reaped.append(tuple(pgids))
        return list(pgids)

    monkeypatch.setattr(runner_mod, "reap_process_group", _fake_reap)

    # e2e-verifier forces isolation + process_lifecycle="bound".
    runner = _runner(factory=_RaisingFactory(), isolator=FakeIsolator())
    with pytest.raises(RuntimeError, match="engine boom"):
        await runner.spawn(SpawnRequest(subagent_type="e2e-verifier", task="t"), _ctx(tmp_path))
    assert reaped == [(424242,)]


class _RecordingIsolator(FakeIsolator):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.finalize_actions: list[Any] = []

    async def finalize(self, iso: Any, action: Any) -> Any:
        self.finalize_actions.append(action)
        return await super().finalize(iso, action)


async def test_S8_tainted_finalize_discards_not_autocommit(tmp_path: Path) -> None:
    # §7.3: tainted audit must NOT autocommit polluted tracked changes; use DISCARD.
    from autoverse.ports.isolation import FinalizeAction

    iso = _RecordingIsolator(audit_tainted=True)
    factory = FakeEngineFactory(RunOutcome(summary="probed", verdict="PASS"))
    runner = _runner(factory=factory, isolator=iso)
    res = await runner.spawn(SpawnRequest(subagent_type="e2e-verifier", task="t"), _ctx(tmp_path))
    assert iso.calls.index("audit") < iso.calls.index("finalize")
    assert iso.finalize_actions == [FinalizeAction.DISCARD]
    assert res.tainted is True
    assert res.verdict is None


async def test_clean_audit_still_autocommits(tmp_path: Path) -> None:
    from autoverse.ports.isolation import FinalizeAction

    iso = _RecordingIsolator(audit_tainted=False)
    factory = FakeEngineFactory(RunOutcome(summary="probed", verdict="PASS"))
    runner = _runner(factory=factory, isolator=iso)
    await runner.spawn(SpawnRequest(subagent_type="e2e-verifier", task="t"), _ctx(tmp_path))
    assert iso.finalize_actions == [FinalizeAction.AUTOCOMMIT]


# ── lifecycle events (display §13) ──────────────────────────────────────────


def _runner_with_sink(sink: Any, *, factory: Any = None, job_store: Any = None) -> SubAgentRunner:
    return SubAgentRunner(
        engine_factory=factory or FakeEngineFactory(RunOutcome(summary="ok")),
        isolator=FakeIsolator(),
        resource_lock=ResourceLock(),
        job_store=job_store or InMemoryJobStore(),
        events=sink,
    )


def _activity(events: list[Any]) -> list[tuple[str, str, str]]:
    """(activity, status, job_id) for every SUBAGENT_ACTIVITY emit, in order."""
    from autoverse.types.events import EventType

    out: list[tuple[str, str, str]] = []
    for e in events:
        if getattr(e, "type", None) is EventType.SUBAGENT_ACTIVITY:
            p = e.payload
            out.append((p.activity, p.status, p.job_id))
    return out


async def test_foreground_emits_running_then_done_with_stable_id(tmp_path: Path) -> None:
    sink = _Sink()
    runner = _runner_with_sink(sink, factory=FakeEngineFactory(RunOutcome(summary="found it")))
    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), _ctx(tmp_path))

    acts = _activity(sink.events)
    statuses = [s for _, s, _ in acts]
    assert statuses == ["running", "done"]
    # Both lifecycle events share ONE stable dispatch id (panel keying).
    ids = {jid for _, _, jid in acts}
    assert len(ids) == 1 and next(iter(ids)).startswith("sa-")


async def test_failed_execution_emits_failed_lifecycle(tmp_path: Path) -> None:
    class _BoomIsolator(FakeIsolator):
        def scope(self, *a: Any, **k: Any) -> Any:  # type: ignore[override]
            # scope() is a CM factory; raising here fails the dispatch before the
            # body runs (fail-closed isolation, §3.2/S6).
            raise IsolationError("cannot create worktree")

    sink = _Sink()
    runner = SubAgentRunner(
        engine_factory=FakeEngineFactory(RunOutcome(summary="never")),
        isolator=_BoomIsolator(),
        resource_lock=ResourceLock(),
        job_store=InMemoryJobStore(),
        events=sink,
    )
    with pytest.raises(IsolationError):
        # e2e-verifier forces isolation → the boom isolator fails the dispatch.
        await runner.spawn(SpawnRequest(subagent_type="e2e-verifier", task="t"), _ctx(tmp_path))

    statuses = [s for _, s, _ in _activity(sink.events)]
    assert statuses == ["running", "failed"]


# ── plan-mode capability downgrade (docs/guides/using-cli-and-tui.md §3.10.2) ─────────────────


def _plan_ctx(cwd: Path, *, spawn: Any = None) -> ToolContext:
    """A ToolContext whose parent session is in plan mode."""
    ctx = _ctx(cwd, spawn=spawn)
    ctx.app.extra["plan_mode"] = {"active": True, "reason": "user toggled plan mode"}
    return ctx


_WRITE_TIER_TOOLS = {"write_file", "edit_file", "delete_file", "notebook_edit", "bash"}


async def test_plan_mode_downgrades_builder_to_read_only(tmp_path: Path) -> None:
    # In plan mode a write-tier sub-agent (builder) must be clamped to READ_ONLY
    # so its write/exec tools are dropped — otherwise it bypasses the plan gate.
    factory = FakeEngineFactory(RunOutcome(summary="done"))
    runner = _runner(factory=factory)
    await runner.spawn(SpawnRequest(subagent_type="builder", task="do X"), _plan_ctx(tmp_path))

    c = factory.constructions[-1]
    names = set(c["tools"].names()) if c.get("tools") else set()
    assert names.isdisjoint(_WRITE_TIER_TOOLS), f"write tools leaked: {names & _WRITE_TIER_TOOLS}"
    assert "## Plan Mode (parent)" in c["system_prompt"]


async def test_plan_mode_does_not_affect_scout(tmp_path: Path) -> None:
    # Read-only roles are unaffected: same tool set in plan and normal mode, and
    # no downgrade banner (they were never downgraded).
    factory = FakeEngineFactory(RunOutcome(summary="done"))
    runner = _runner(factory=factory)

    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), _ctx(tmp_path))
    normal_names = sorted(factory.constructions[-1]["tools"].names())

    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), _plan_ctx(tmp_path))
    plan_c = factory.constructions[-1]
    plan_names = sorted(plan_c["tools"].names())

    assert plan_names == normal_names
    assert "## Plan Mode (parent)" not in plan_c["system_prompt"]


async def test_plan_mode_exempts_checkpoint_writer(tmp_path: Path) -> None:
    # checkpoint-writer is a system role (CONTROLLED_WRITE) exempt from the
    # downgrade: its write tools must survive even in plan mode.
    factory = FakeEngineFactory(RunOutcome(summary="done"))
    runner = _runner(factory=factory)
    await runner.write_checkpoint(_plan_ctx(tmp_path), task="reconcile memory")

    c = factory.constructions[-1]
    names = set(c["tools"].names()) if c.get("tools") else set()
    assert {"write_file", "edit_file"} <= names
    assert "## Plan Mode (parent)" not in c["system_prompt"]


async def test_normal_mode_builder_keeps_write_tools(tmp_path: Path) -> None:
    # Sanity guard: outside plan mode, builder retains its full write-tier set.
    factory = FakeEngineFactory(RunOutcome(summary="done"))
    runner = _runner(factory=factory)
    await runner.spawn(SpawnRequest(subagent_type="builder", task="do X"), _ctx(tmp_path))

    c = factory.constructions[-1]
    names = set(c["tools"].names()) if c.get("tools") else set()
    assert {"write_file", "edit_file", "bash"} <= names
    assert "## Plan Mode (parent)" not in c["system_prompt"]
