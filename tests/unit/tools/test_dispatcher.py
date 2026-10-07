"""Unit: ToolDispatcher middleware chain (T2, T3, T4, T5 + concurrency)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from autoverse.core.tools import ToolDispatcher, ToolRegistry, register_tool
from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.infra.permission.fixed import (
    AllowAllPermissionAdapter,
    DenyAllPermissionAdapter,
)
from autoverse.types.abort import AbortController
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import EventType
from autoverse.types.tools import ToolCall, ToolResult

from tests.conftest import RecordingSink, make_ctx


class EchoArgs(BaseModel):
    value: str


class EchoTool(BaseTool):
    name = "echo"
    description = "echo"
    capability = CapabilityTier.READ_ONLY
    Args = EchoArgs

    async def run(self, args: EchoArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok(args.value)


class SlowArgs(BaseModel):
    delay: float = 0.05
    timeout: float | None = None


class SlowTool(BaseTool):
    name = "slow"
    description = "slow"
    capability = CapabilityTier.READ_ONLY
    Args = SlowArgs

    async def run(self, args: SlowArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.delay)
        return ToolResult.ok("done")


class BoomArgs(BaseModel):
    pass


class BoomTool(BaseTool):
    name = "boom"
    description = "boom"
    capability = CapabilityTier.READ_ONLY
    Args = BoomArgs

    async def run(self, args: BoomArgs, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("internal detail leak")


def _registry() -> ToolRegistry:
    return ToolRegistry.build(extra=[EchoTool(), SlowTool(), BoomTool()])


async def test_t2_unknown_tool_lists_available(tmp_path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    res = await disp.dispatch_one(ToolCall("1", "nope", {}), ctx)
    assert res.is_error and res.error_kind == "unknown_tool"
    assert "echo" in res.output


async def test_t2_validation_error_skips_run(tmp_path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    res = await disp.dispatch_one(ToolCall("1", "echo", {}), ctx)
    assert res.is_error and res.error_kind == "invalid_args"


async def test_t4_emits_start_and_end_on_allow(tmp_path) -> None:
    sink = RecordingSink()
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path, sink=sink)
    await disp.dispatch_one(ToolCall("1", "echo", {"value": "hi"}), ctx)
    starts = sink.of_type(EventType.TOOL_START)
    ends = sink.of_type(EventType.TOOL_END)
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].payload.decision == "allow"


async def test_t3_deny_still_emits_start_deny_and_end(tmp_path) -> None:
    sink = RecordingSink()
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path, sink=sink, permissions=DenyAllPermissionAdapter())
    res = await disp.dispatch_one(ToolCall("1", "echo", {"value": "x"}), ctx)
    assert res.is_error and res.error_kind == "permission_denied"
    starts = sink.of_type(EventType.TOOL_START)
    ends = sink.of_type(EventType.TOOL_END)
    assert starts[0].payload.decision == "deny"
    assert len(ends) == 1


async def test_t5_timeout(tmp_path) -> None:
    disp = ToolDispatcher(_registry(), default_timeout=120)
    ctx = make_ctx(tmp_path)
    res = await disp.dispatch_one(
        ToolCall("1", "slow", {"delay": 1.0, "timeout": 0.01}), ctx
    )
    assert res.is_error and res.error_kind == "timeout"


async def test_t5_abort(tmp_path) -> None:
    disp = ToolDispatcher(_registry())
    ctrl = AbortController()
    ctx = make_ctx(tmp_path, controller=ctrl)

    async def aborter() -> None:
        await asyncio.sleep(0.01)
        ctrl.abort("interrupt")

    task = asyncio.create_task(
        disp.dispatch_one(ToolCall("1", "slow", {"delay": 5.0}), ctx)
    )
    await aborter()
    res = await task
    assert res.is_error and res.error_kind == "aborted"
    assert res.metadata.get("interrupted") is True


async def test_t5_unexpected_error_contained(tmp_path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    res = await disp.dispatch_one(ToolCall("1", "boom", {}), ctx)
    assert res.is_error and res.error_kind == "tool_error"
    assert "Traceback" not in res.output


class _TierCapturingPermissions:
    """PermissionPort double that records the tier of every checked call."""

    def __init__(self) -> None:
        self.seen_tiers: list[CapabilityTier] = []

    async def check(self, call: ToolCall) -> "Decision":
        from autoverse.types.tools import Decision

        self.seen_tiers.append(call.tier)
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> "Decision":
        from autoverse.types.tools import Decision

        return Decision.allow()

    async def request_approval(self, call: ToolCall) -> "Decision":
        from autoverse.types.tools import Decision

        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class ProcArgs(BaseModel):
    pass


class ProcTool(BaseTool):
    name = "proc"
    description = "proc"
    capability = CapabilityTier.PROCESS
    Args = ProcArgs

    async def run(self, args: ProcArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("ran")


async def test_dispatch_wires_tier_from_capability(tmp_path) -> None:
    perms = _TierCapturingPermissions()
    registry = ToolRegistry.build(extra=[ProcTool()])
    disp = ToolDispatcher(registry)
    ctx = make_ctx(tmp_path, permissions=perms)  # type: ignore[arg-type]
    # turn_loop constructs ToolCall without a tier -> defaults to READ_ONLY.
    await disp.dispatch_one(ToolCall("1", "proc", {}), ctx)
    assert perms.seen_tiers == [CapabilityTier.PROCESS]


async def test_t3_concurrent_dispatch_preserves_order(tmp_path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    calls = [
        ToolCall("1", "slow", {"delay": 0.05}),
        ToolCall("2", "echo", {"value": "fast"}),
        ToolCall("3", "slow", {"delay": 0.02}),
    ]
    results = await disp.dispatch(calls, ctx)
    assert len(results) == 3
    assert results[1].output == "fast"  # order preserved despite differing delays


# ── plan-mode gate (docs/guides/using-cli-and-tui.md §3.10, docs/reference/tools.md §5.5) ────────────────────


class WriteArgs(BaseModel):
    value: str = "x"


class WriteTool(BaseTool):
    name = "writer"
    description = "write-tier tool"
    capability = CapabilityTier.CONTROLLED_WRITE
    Args = WriteArgs

    async def run(self, args: WriteArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("wrote")


class ExitPlanArgs(BaseModel):
    plan: str = "do it"


class ExitPlanTool(BaseTool):
    name = "ExitPlanMode"
    description = "exit plan mode"
    capability = CapabilityTier.READ_ONLY
    Args = ExitPlanArgs

    async def run(self, args: ExitPlanArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("exited")


def _plan_registry() -> ToolRegistry:
    # include=[] excludes all builtins (notably the real ExitPlanMode) so the
    # fake ExitPlanTool below does not collide on name.
    return ToolRegistry.build(include=[], extra=[EchoTool(), WriteTool(), ExitPlanTool()])


async def test_plan_mode_blocks_write_tier_tool(tmp_path) -> None:
    sink = RecordingSink()
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path, sink=sink)
    ctx.app.extra["plan_mode"] = {"active": True, "reason": "r"}
    res = await disp.dispatch_one(ToolCall("1", "writer", {"value": "x"}), ctx)
    assert res.is_error and res.error_kind == "plan_mode_blocked"
    # Emits the same deny-shaped TOOL_START/TOOL_END pair as a permission deny.
    starts = sink.of_type(EventType.TOOL_START)
    ends = sink.of_type(EventType.TOOL_END)
    assert starts and starts[0].payload.decision == "deny"
    assert ends and ends[0].payload.is_error


async def test_plan_mode_allows_read_only_tool(tmp_path) -> None:
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path)
    ctx.app.extra["plan_mode"] = {"active": True}
    res = await disp.dispatch_one(ToolCall("1", "echo", {"value": "hi"}), ctx)
    assert not res.is_error and res.output == "hi"


async def test_plan_mode_allows_exit_plan_mode(tmp_path) -> None:
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path)
    ctx.app.extra["plan_mode"] = {"active": True}
    res = await disp.dispatch_one(ToolCall("1", "ExitPlanMode", {"plan": "p"}), ctx)
    assert not res.is_error and res.output == "exited"


async def test_plan_mode_inactive_does_not_block(tmp_path) -> None:
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path)
    # No plan_mode key at all -> normal mode, write tool runs.
    res = await disp.dispatch_one(ToolCall("1", "writer", {"value": "x"}), ctx)
    assert not res.is_error and res.output == "wrote"
    # Explicit active=False is also normal mode.
    ctx.app.extra["plan_mode"] = {"active": False}
    res2 = await disp.dispatch_one(ToolCall("2", "writer", {"value": "x"}), ctx)
    assert not res2.is_error and res2.output == "wrote"


async def test_pending_approval_blocks_all_tools(tmp_path) -> None:
    """After ExitPlanMode, pending_approval state blocks even READ_ONLY tools."""
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path)
    ctx.app.extra["plan_mode"] = {"active": False, "pending_approval": True}
    res = await disp.dispatch_one(ToolCall("1", "echo", {"value": "hi"}), ctx)
    assert res.is_error and res.error_kind == "plan_pending_approval"


async def test_exit_plan_mode_batch_barrier(tmp_path) -> None:
    """ExitPlanMode mixed with other tools in a batch: only ExitPlanMode runs."""
    disp = ToolDispatcher(_plan_registry())
    ctx = make_ctx(tmp_path)
    ctx.app.extra["plan_mode"] = {"active": True}
    calls = [
        ToolCall("1", "ExitPlanMode", {"plan": "1. do\n2. thing"}),
        ToolCall("2", "writer", {"value": "x"}),
    ]
    results = await disp.dispatch(calls, ctx)
    assert not results[0].is_error and results[0].output == "exited"
    assert results[1].is_error and results[1].error_kind == "plan_mode_batch_barrier"
