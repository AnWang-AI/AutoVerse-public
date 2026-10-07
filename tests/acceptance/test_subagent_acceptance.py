"""Phase 4 acceptance — sub-agent + isolation S1–S11 (docs/reference/testing.md §6).

| S1 forced explicit type | S2 capability ceiling | S3 check_mode injection
| S4 fg/bg one path       | S5 background fan-in   | S6 worktree fail-closed
| S7 resource-lock deadlock-free | S8 post-audit taint | S9 process reclamation
| S10 overrun non-empty summary | S11 prompt mechanism mapping
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import autoverse.core.tools.builtin  # noqa: F401 — trigger registration
from autoverse.core.subagent.definition import AGENT_DEFINITIONS, get_definition
from autoverse.core.subagent.filter import CheckModeBashTool, filter_registry
from autoverse.core.subagent.jobs import InMemoryJobStore, ResourceLock, reap_process_group
from autoverse.core.subagent.runner import RunOutcome, SpawnRequest
from autoverse.core.tools.registry import ToolRegistry
from autoverse.ports.isolation import IsolationError
from autoverse.types.capability import CapabilityTier

from tests._doubles import FakeEngineFactory, FakeIsolator
from tests.acceptance.conftest import make_runner, make_subagent_ctx


# ── S1 — forced explicit subagent_type ────────────────────────────────────────
async def test_S1_spawn_requires_explicit_type(tmp_path: Path) -> None:
    # schema makes subagent_type required (no string-route guessing)
    with pytest.raises(ValidationError):
        SpawnRequest(task="do work")  # type: ignore[call-arg]
    # defensive ValueError if an empty type sneaks past the schema
    runner = make_runner()
    req = SpawnRequest.model_construct(subagent_type="", task="x")
    with pytest.raises(ValueError):
        await runner.spawn(req, make_subagent_ctx(cwd=tmp_path))


# ── S2 — capability ceiling filtering (scout) ─────────────────────────────────
async def test_S2_capability_ceiling_blocks_bash_and_web(tmp_path: Path) -> None:
    sub = filter_registry(ToolRegistry.build(), get_definition("scout"))
    names = set(sub.names())
    assert "bash" not in names
    assert "web_fetch" not in names and "web_search" not in names
    assert names <= {"read_file", "glob", "grep"}


# ── S3 — check_mode injection (validator) ─────────────────────────────────────
async def test_S3_check_mode_injection(tmp_path: Path) -> None:
    sub = filter_registry(ToolRegistry.build(), get_definition("validator"))
    assert isinstance(sub.get("bash"), CheckModeBashTool)
    # P8 disclosure present
    assert "bash runs in check_mode" in AGENT_DEFINITIONS["validator"].system_prompt.lower()


# ── S4 — foreground / background share one execution path ─────────────────────
async def test_S4_fg_bg_one_path(tmp_path: Path) -> None:
    factory = FakeEngineFactory(RunOutcome(summary="ok"))
    runner = make_runner(engine_factory=factory)

    await runner.spawn(SpawnRequest(subagent_type="scout", task="t"), make_subagent_ctx(cwd=tmp_path))
    fg = dict(factory.constructions[-1])

    launched: list[Any] = []
    ctx = make_subagent_ctx(cwd=tmp_path, spawn=launched.append)
    res = await runner.spawn(
        SpawnRequest(subagent_type="scout", task="t", run_in_background=True), ctx
    )
    assert res.trace_id and res.trace_id.startswith("job-")
    assert len(launched) == 1
    await launched[0]
    bg = dict(factory.constructions[-1])

    def norm(c: dict[str, Any]) -> dict[str, Any]:
        # dispatch_id is unique per spawn (panel keying) → excluded from the check.
        out = {k: v for k, v in c.items() if k not in ("cwd", "tools", "dispatch_id")}
        out["tool_names"] = sorted(c["tools"].names()) if c.get("tools") else None
        return out

    assert norm(fg) == norm(bg)


# ── S5 — background fan-in ─────────────────────────────────────────────────────
async def test_S5_background_fan_in(tmp_path: Path) -> None:
    jobs = InMemoryJobStore()
    factory = FakeEngineFactory(RunOutcome(summary="child-done"))
    runner = make_runner(engine_factory=factory, job_store=jobs)

    launched: list[Any] = []
    ctx = make_subagent_ctx(cwd=tmp_path, spawn=launched.append)

    for i in range(3):
        await runner.spawn(
            SpawnRequest(subagent_type="scout", task=f"t{i}", run_in_background=True), ctx
        )
    assert len(launched) == 3
    # run two of the three to completion
    await launched[0]
    await launched[1]

    finished = await jobs.collect_finished(block=True, timeout=1.0)
    assert len(finished) == 2
    assert all(r.summary == "child-done" for r in finished)
    # the third still pending; reappears next collect
    assert jobs.has_pending() is True
    await launched[2]
    again = await jobs.collect_finished(block=True, timeout=1.0)
    assert len(again) == 1


# ── S6 — worktree strong isolation, fail-closed ───────────────────────────────
async def test_S6_worktree_fail_closed(tmp_path: Path) -> None:
    # FakeIsolator.scope raises IsolationError → runner has NO degrade branch.
    runner = make_runner(isolator=FakeIsolator(scope_error=True))
    with pytest.raises(IsolationError):
        await runner.spawn(
            SpawnRequest(subagent_type="builder", task="t", isolation="worktree"),
            make_subagent_ctx(cwd=tmp_path),
        )


async def test_S6_worktree_writes_land_in_box(tmp_path: Path) -> None:
    box = tmp_path / "box"
    box.mkdir()
    iso = FakeIsolator(cwd=box)
    # capture the cwd the engine is constructed with
    factory = FakeEngineFactory(RunOutcome(summary="built"))
    runner = make_runner(engine_factory=factory, isolator=iso)
    await runner.spawn(
        SpawnRequest(subagent_type="builder", task="t", isolation="worktree"),
        make_subagent_ctx(cwd=tmp_path),
    )
    assert factory.constructions[-1]["cwd"] == box
    assert "scope" in iso.calls


# ── S7 — resource lock deadlock prevention ────────────────────────────────────
async def test_S7_resource_lock_no_deadlock(tmp_path: Path) -> None:
    lock = ResourceLock()
    runner = make_runner(resource_lock=lock)
    ctx = make_subagent_ctx(cwd=tmp_path)

    async def go(resources: tuple[str, ...]) -> str:
        res = await runner.spawn(
            SpawnRequest(subagent_type="scout", task="t", exclusive_resources=resources), ctx
        )
        return res.summary

    # two spawns contending the same two resources in opposite order → sorted acquire
    out = await asyncio.wait_for(
        asyncio.gather(go(("db", "port")), go(("port", "db"))), timeout=2.0
    )
    assert len(out) == 2


# ── S8 — post-execution audit (e2e-verifier) ──────────────────────────────────
async def test_S8_post_audit_taints_verdict(tmp_path: Path) -> None:
    iso = FakeIsolator(audit_tainted=True)
    factory = FakeEngineFactory(RunOutcome(summary="ran probes", verdict="PASS"))
    runner = make_runner(engine_factory=factory, isolator=iso)
    res = await runner.spawn(
        SpawnRequest(subagent_type="e2e-verifier", task="verify"), make_subagent_ctx(cwd=tmp_path)
    )
    # audit BEFORE finalize (autocommit would hide the diff)
    assert iso.calls.index("audit") < iso.calls.index("finalize")
    assert res.tainted is True
    assert res.verdict is None
    assert "VERIFICATION_TAINTED" in res.summary


# ── S9 — bound process reclamation ────────────────────────────────────────────
async def test_S9_bound_process_reclamation(tmp_path: Path) -> None:
    import subprocess

    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(proc.pid)

    # engine reports the bound process group; runner must reap it (e2e-verifier=bound)
    factory = FakeEngineFactory(RunOutcome(summary="done", pgids=(pgid,)))
    iso = FakeIsolator()
    runner = make_runner(engine_factory=factory, isolator=iso)
    await runner.spawn(
        SpawnRequest(subagent_type="e2e-verifier", task="t"), make_subagent_ctx(cwd=tmp_path)
    )
    proc.wait(timeout=5)
    assert proc.returncode is not None  # process was killed, not leaked


# ── S10 — overrun produces a non-empty progress summary ───────────────────────
async def test_S10_overrun_nonempty_summary(tmp_path: Path) -> None:
    factory = FakeEngineFactory(RunOutcome(summary="", degraded="max_turns"))
    runner = make_runner(engine_factory=factory)
    res = await runner.spawn(
        SpawnRequest(subagent_type="builder", task="t"), make_subagent_ctx(cwd=tmp_path)
    )
    assert res.degraded == "max_turns"
    assert res.summary.strip() != ""
    assert "max_turns" in res.summary


async def test_S10_wall_clock_timeout_degrades(tmp_path: Path) -> None:
    # a hanging engine + tiny wall_clock → TimeoutError → degraded non-empty summary
    factory = FakeEngineFactory(RunOutcome(summary="never"), hang=True)
    runner = make_runner(engine_factory=factory)
    # monkeypatch the role's wall_clock to a tiny value via a custom definition
    from autoverse.core.subagent import definition as defn

    original = defn.AGENT_DEFINITIONS["scout"]
    patched = defn.AgentDefinition(
        name=original.name,
        description=original.description,
        max_tier=original.max_tier,
        can_network=original.can_network,
        bash_check_mode=original.bash_check_mode,
        max_turns=original.max_turns,
        wall_clock=0.05,
        system_prompt=original.system_prompt,
        tools=original.tools,
    )
    defn.AGENT_DEFINITIONS["scout"] = patched
    try:
        res = await runner.spawn(
            SpawnRequest(subagent_type="scout", task="t"), make_subagent_ctx(cwd=tmp_path)
        )
    finally:
        defn.AGENT_DEFINITIONS["scout"] = original
    assert res.degraded == "wall_clock"
    assert res.summary.strip() != ""


# ── S11 — mechanism mapping in prompts (契约一致性) ────────────────────────────
def test_S11_prompt_contract_mechanism_mapping() -> None:
    for name, d in AGENT_DEFINITIONS.items():
        p = d.system_prompt
        pl = p.lower()
        if d.isolation and d.isolation.post_audit:
            assert ("audit" in pl or "审计" in pl) and "verification_tainted" in pl, name
        if d.isolation and d.isolation.process_lifecycle == "bound":
            assert "detach" in pl or "nohup" in pl, name
        if d.isolation and d.isolation.force_isolation:
            assert "worktree" in pl or "isolat" in pl or "隔离" in pl, name
        if d.max_tier < CapabilityTier.PROCESS or d.bash_check_mode:
            assert ("NO " in p or "Do NOT" in p or "do not" in p or "check_mode" in p), name
        if d.bash_check_mode:
            assert "bash runs in check_mode" in pl, name
