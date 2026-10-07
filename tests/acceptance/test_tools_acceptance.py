"""Acceptance: tool subsystem T1-T12 (docs/reference/tools.md)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from autoverse.core.tools import ToolDispatcher, ToolRegistry
from autoverse.core.tools.base import BaseTool, Tool
from autoverse.core.tools.builtin.browser import clamp_screenshot
from autoverse.core.tools.builtin.files import FileEditTool, FileReadTool
from autoverse.core.tools.builtin.shell import BashTool, classify_command
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.pathresolve import is_within
from autoverse.infra.permission.fixed import DenyAllPermissionAdapter
from autoverse.infra.observ.collector import TraceCollector
from autoverse.infra.observ.model import ErrorKind
from autoverse.ports import BrowserError, BrowserPort
from autoverse.types.abort import AbortController
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import EventType, TurnEndPayload, TurnStartPayload, make_event
from autoverse.types.tools import ToolCall, ToolResult

from tests.conftest import RecordingSink, make_ctx

import autoverse.core.tools.builtin  # noqa: F401 — trigger registration


class _SlowArgs(BaseModel):
    delay: float = 5.0
    timeout: float | None = None


class _SlowTool(BaseTool):
    name = "acc_slow"
    description = "slow"
    capability = CapabilityTier.READ_ONLY
    Args = _SlowArgs

    async def run(self, args: _SlowArgs, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(args.delay)
        return ToolResult.ok("done")


def _registry() -> ToolRegistry:
    return ToolRegistry.build(extra=[_SlowTool()])


# T1: tool schema derived from Args.
def test_t1_schema_derivation() -> None:
    api = FileReadTool.to_api_schema()
    assert api["name"] == "read_file"
    assert api["input_schema"] == FileReadTool.Args.model_json_schema()


# T2: routing + validation.
async def test_t2_routing_and_validation(tmp_path: Path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    assert (await disp.dispatch_one(ToolCall("1", "missing", {}), ctx)).error_kind == "unknown_tool"
    assert (await disp.dispatch_one(ToolCall("2", "read_file", {}), ctx)).error_kind == "invalid_args"


# T3: deny still emits start(deny)+end; concurrency preserves order.
async def test_t3_deny_events_and_order(tmp_path: Path) -> None:
    sink = RecordingSink()
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path, sink=sink, permissions=DenyAllPermissionAdapter())
    res = await disp.dispatch_one(ToolCall("1", "think", {"thought": "x"}), ctx)
    assert res.error_kind == "permission_denied"
    assert sink.of_type(EventType.TOOL_START)[0].payload.decision == "deny"
    assert len(sink.of_type(EventType.TOOL_END)) == 1


async def test_t3_concurrency_order(tmp_path: Path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    calls = [
        ToolCall("1", "acc_slow", {"delay": 0.05}),
        ToolCall("2", "think", {"thought": "fast"}),
        ToolCall("3", "acc_slow", {"delay": 0.01}),
    ]
    results = await disp.dispatch(calls, ctx)
    assert len(results) == 3 and results[1].succeeded


# T4: allow path emits start(allow)+end.
async def test_t4_allow_events(tmp_path: Path) -> None:
    sink = RecordingSink()
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path, sink=sink)
    await disp.dispatch_one(ToolCall("1", "think", {"thought": "x"}), ctx)
    assert sink.of_type(EventType.TOOL_START)[0].payload.decision == "allow"
    assert len(sink.of_type(EventType.TOOL_END)) == 1


# T5: timeout + abort + error containment.
async def test_t5_timeout(tmp_path: Path) -> None:
    disp = ToolDispatcher(_registry())
    ctx = make_ctx(tmp_path)
    res = await disp.dispatch_one(ToolCall("1", "acc_slow", {"delay": 1.0, "timeout": 0.01}), ctx)
    assert res.error_kind == "timeout"


async def test_t5_abort(tmp_path: Path) -> None:
    disp = ToolDispatcher(_registry())
    ctrl = AbortController()
    ctx = make_ctx(tmp_path, controller=ctrl)
    task = asyncio.create_task(disp.dispatch_one(ToolCall("1", "acc_slow", {"delay": 5.0}), ctx))
    await asyncio.sleep(0.01)
    ctrl.abort("cancel")
    res = await task
    assert res.error_kind == "aborted" and res.metadata.get("cancelled") is True


async def test_browser_evaluate_failure_is_preserved_in_trace(tmp_path: Path) -> None:
    collector = TraceCollector("browser-trace", "test-model")
    await collector.emit(
        make_event(TurnStartPayload(turn_index=0, iteration=0, model="test-model"))
    )
    backend = AsyncMock(spec=BrowserPort)
    backend.evaluate.side_effect = BrowserError(
        "JavaScript returned an unsupported value; use `return await ...`.",
        error_kind="unsupported_result",
    )
    ctx = make_ctx(tmp_path, sink=collector)  # type: ignore[arg-type]
    ctx.app.browser = backend

    result = await ToolDispatcher(_registry()).dispatch_one(
        ToolCall(
            "browser-evaluate-1",
            "browser_evaluate",
            {"script": "Promise.resolve('unsupported')"},
        ),
        ctx,
    )

    assert result.error_kind == "unsupported_result"
    await collector.emit(
        make_event(TurnEndPayload(turn_index=0, iterations=1, stop_cause="end_turn"))
    )
    record = collector.model.turns[0].tools[0]
    assert record.name == "browser_evaluate"
    assert record.error_kind is ErrorKind.UNSUPPORTED_RESULT
    assert "return await" in record.error_message


# T6: edit no-match guidance.
async def test_t6_edit_guidance(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hello")
    ctx = make_ctx(tmp_path)
    await FileReadTool().run(FileReadTool.Args(file_path=str(f)), ctx)
    res = await FileEditTool().run(
        FileEditTool.Args(file_path=str(f), old_string="nope", new_string="x"), ctx
    )
    assert res.error_kind == "no_match" and "Re-read" in res.output


# T7: command classification.
def test_t7_command_classification() -> None:
    assert classify_command("rm -rf /")[0] == "destructive_delete"
    assert classify_command("ls")[1] is True


# T8: bash check_mode.
async def test_t8_check_mode(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    blocked = await BashTool().run(BashTool.Args(command="rm -rf x", check_mode=True), ctx)
    allowed = await BashTool().run(BashTool.Args(command="ls", check_mode=True), ctx)
    assert blocked.is_error and allowed.succeeded


# T9: registry build + runtime register.
def test_t9_registry() -> None:
    reg = ToolRegistry.build(include={"think"})
    assert reg.names() == ["think"]
    handle = reg.register("ext", [_SlowTool()])
    assert "acc_slow" in reg.names()
    handle.dispose()
    assert "acc_slow" not in reg.names()


# T10: structural Protocol.
def test_t10_protocol() -> None:
    assert isinstance(FileReadTool(), Tool)


# T11: workspace containment.
def test_t11_containment() -> None:
    assert is_within(Path("/work"), Path("/work/sub")) is True
    assert is_within(Path("/work"), Path("/etc")) is False


# T12: screenshot guard.
def test_t12_screenshot_guard() -> None:
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (1, 2, 3)).save(buf, format="PNG")
    data, meta = clamp_screenshot(buf.getvalue())
    assert data is not None and len(data) <= 150_000
