"""EnterPlanMode idempotency (plan.py).

Calling ``EnterPlanMode`` while plan mode is already active should NOT overwrite
the existing state or pretend to re-enter. It returns a hint telling the model
to continue read-only research and call ``ExitPlanMode`` once, preserving the
original ``reason``. Entering from an inactive/absent state behaves as before.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from autoverse.core.tools.builtin.plan import EnterPlanModeArgs, EnterPlanModeTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.tracker import FileTracker
from autoverse.types import AbortController, Decision
from autoverse.types.tools import ToolCall


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
    async def emit(self, event: object) -> None:  # pragma: no cover - unused here
        return None


def _ctx(extra: dict[str, object]) -> ToolContext:
    return ToolContext(
        cwd=Path("."),
        permissions=_AllowAll(),
        tracker=FileTracker(),
        signal=AbortController().signal,
        events=_Sink(),
        app=SimpleNamespace(extra=extra),
    )


_ARGS = EnterPlanModeArgs(reason="user toggled plan mode")


async def test_enter_plan_mode_when_already_active_returns_hint() -> None:
    extra: dict[str, object] = {
        "plan_mode": {"active": True, "reason": "original reason", "pending_approval": False}
    }
    tool = EnterPlanModeTool()

    result = await tool.run(_ARGS, _ctx(extra))

    assert not result.is_error
    # hint wording, not the fresh-enter message
    assert "Already in plan mode" in result.output
    assert result.metadata.get("already_active") is True
    # state preserved — reason NOT overwritten
    assert extra["plan_mode"]["reason"] == "original reason"
    assert extra["plan_mode"]["active"] is True


async def test_enter_plan_mode_from_inactive_sets_active() -> None:
    extra: dict[str, object] = {"plan_mode": {"active": False}}
    tool = EnterPlanModeTool()

    result = await tool.run(_ARGS, _ctx(extra))

    assert not result.is_error
    assert "Entered plan mode" in result.output
    assert result.metadata.get("already_active") is None
    assert extra["plan_mode"]["active"] is True
    assert extra["plan_mode"]["reason"] == "user toggled plan mode"


async def test_enter_plan_mode_from_absent_sets_active() -> None:
    extra: dict[str, object] = {}
    tool = EnterPlanModeTool()

    result = await tool.run(_ARGS, _ctx(extra))

    assert not result.is_error
    assert "Entered plan mode" in result.output
    assert extra["plan_mode"]["active"] is True
