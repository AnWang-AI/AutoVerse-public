from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

from pydantic import BaseModel

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.dispatcher import ToolDispatcher
from autoverse.core.tools.registry import ToolRegistry
from autoverse.core.tools.tracker import FileTracker
from autoverse.types import AbortController, Decision, StreamEvent
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolCall, ToolResult


class _Args(BaseModel):
    delay: float = 0.0


class _AgentLikeTool(BaseTool):
    name: ClassVar[str] = "agent"
    description: ClassVar[str] = "Test stand-in for the agent tool."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _Args

    async def run(self, args: _Args, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.delay)
        return ToolResult.ok("agent finished")


class _SlowTool(_AgentLikeTool):
    name: ClassVar[str] = "slow"
    description: ClassVar[str] = "Regular slow tool."

    async def run(self, args: _Args, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.delay)
        return ToolResult.ok("slow finished")


class _AskUserQuestionLikeTool(_AgentLikeTool):
    name: ClassVar[str] = "AskUserQuestion"
    description: ClassVar[str] = "Test stand-in for the user question tool."

    async def run(self, args: _Args, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.delay)
        return ToolResult.ok("answered")


class _WriteArgs(BaseModel):
    pass


class _WriteTool(BaseTool):
    name: ClassVar[str] = "write_file"
    description: ClassVar[str] = "Write-like test tool."
    capability: ClassVar[CapabilityTier] = CapabilityTier.CONTROLLED_WRITE
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _WriteArgs

    async def run(self, args: _WriteArgs, ctx: ToolContext) -> ToolResult:
        ctx.app.extra["write_ran"] = True
        return ToolResult.ok("wrote")


class _BashArgs(BaseModel):
    command: str


class _BashTool(BaseTool):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = "Bash-like test tool."
    capability: ClassVar[CapabilityTier] = CapabilityTier.PROCESS
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = _BashArgs

    async def run(self, args: _BashArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("bash ran")


class _BrowserClickTool(BaseTool):
    name: ClassVar[str] = "browser_click"
    description: ClassVar[str] = "Browser click-like test tool."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = True
    Args: ClassVar[type[BaseModel]] = _WriteArgs

    async def run(self, args: _WriteArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("clicked")


class _BrowserScreenshotTool(_BrowserClickTool):
    name: ClassVar[str] = "browser_screenshot"
    description: ClassVar[str] = "Browser screenshot-like test tool."

    async def run(self, args: _WriteArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("screenshot")


class _AllowAll:
    async def check(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def check_sync(self, call: ToolCall) -> Decision:
        return Decision.allow()

    async def request_approval(self, call: ToolCall) -> Decision:
        return Decision.allow()

    def cancel_all(self) -> None:
        return None


class _EventSink:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    async def emit(self, event: StreamEvent) -> None:
        self.events.append(event)


def _ctx(tmp_path: Path, *, extra: dict[str, object] | None = None) -> ToolContext:
    events = _EventSink()
    return ToolContext(
        cwd=tmp_path,
        permissions=_AllowAll(),
        tracker=FileTracker(),
        signal=AbortController().signal,
        events=events,
        app=SimpleNamespace(extra=extra if extra is not None else {}),
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("test", [_AgentLikeTool(), _AskUserQuestionLikeTool(), _SlowTool()])
    return registry


def _plan_registry() -> ToolRegistry:
    from autoverse.core.tools.builtin.plan import ExitPlanModeTool

    registry = ToolRegistry()
    registry.register(
        "test",
        [
            ExitPlanModeTool(),
            _WriteTool(),
            _BashTool(),
            _BrowserClickTool(),
            _BrowserScreenshotTool(),
        ],
    )
    return registry


async def test_agent_tool_does_not_inherit_default_timeout(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(_registry(), default_timeout=0.01)

    result = await dispatcher.dispatch_one(
        ToolCall(id="t1", name="agent", input={"delay": 0.03}),
        _ctx(tmp_path),
    )

    assert result.output == "agent finished"
    assert not result.is_error


async def test_ask_user_question_does_not_inherit_default_timeout(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(_registry(), default_timeout=0.01)

    result = await dispatcher.dispatch_one(
        ToolCall(id="t1", name="AskUserQuestion", input={"delay": 0.03}),
        _ctx(tmp_path),
    )

    assert result.output == "answered"
    assert not result.is_error


async def test_regular_tool_still_inherits_default_timeout(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(_registry(), default_timeout=0.01)

    result = await dispatcher.dispatch_one(
        ToolCall(id="t1", name="slow", input={"delay": 0.03}),
        _ctx(tmp_path),
    )

    assert result.is_error
    assert result.error_kind == "timeout"
    assert "timed out after 0.01s" in result.output


async def test_plan_mode_blocks_write_tools(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, extra={"plan_mode": {"active": True}})
    result = await ToolDispatcher(_plan_registry()).dispatch_one(
        ToolCall(id="w1", name="write_file", input={}),
        ctx,
    )

    assert result.is_error
    assert result.error_kind == "plan_mode_blocked"
    assert "write_ran" not in ctx.app.extra


async def test_plan_mode_allows_readonly_bash_only(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(_plan_registry())
    ctx = _ctx(tmp_path, extra={"plan_mode": {"active": True}})

    readonly = await dispatcher.dispatch_one(
        ToolCall(id="b1", name="bash", input={"command": "git status && pwd"}),
        ctx,
    )
    mutating = await dispatcher.dispatch_one(
        ToolCall(id="b2", name="bash", input={"command": "python script.py"}),
        ctx,
    )

    assert not readonly.is_error
    assert readonly.output == "bash ran"
    assert mutating.is_error
    assert mutating.error_kind == "plan_mode_blocked"


async def test_plan_mode_blocks_external_browser_side_effects(tmp_path: Path) -> None:
    dispatcher = ToolDispatcher(_plan_registry())
    ctx = _ctx(tmp_path, extra={"plan_mode": {"active": True}})

    click = await dispatcher.dispatch_one(
        ToolCall(id="c1", name="browser_click", input={}),
        ctx,
    )
    screenshot = await dispatcher.dispatch_one(
        ToolCall(id="s1", name="browser_screenshot", input={}),
        ctx,
    )

    assert click.is_error
    assert click.error_kind == "plan_mode_blocked"
    assert not screenshot.is_error
    assert screenshot.output == "screenshot"


async def test_exit_plan_mode_is_batch_barrier(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, extra={"plan_mode": {"active": True}})

    results = await ToolDispatcher(_plan_registry()).dispatch(
        [
            ToolCall(id="p1", name="ExitPlanMode", input={"plan": "1. inspect\n2. edit"}),
            ToolCall(id="w1", name="write_file", input={}),
        ],
        ctx,
    )

    assert results[0].succeeded
    assert results[0].metadata["pending_approval"] is True
    assert results[1].is_error
    assert results[1].error_kind == "plan_mode_batch_barrier"
    assert ctx.app.extra["plan_mode"]["pending_approval"] is True
    assert "write_ran" not in ctx.app.extra
