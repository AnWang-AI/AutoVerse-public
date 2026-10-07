"""TurnLoop plan-mode system-prompt injection (docs/guides/using-cli-and-tui.md §3.10).

When ``app.extra["plan_mode"].active`` is set, ``_system_prompt`` appends a
planning-mode section that (a) forbids edits/mutating tools, (b) forbids using
sub-agents as a write bypass, and (c) steers toward AskUserQuestion + a single
converged, numbered ExitPlanMode call. When inactive, none of that appears.
"""

from __future__ import annotations

from tests._doubles import ScriptedProvider
from tests.acceptance.conftest import make_loop
from autoverse.core.loop.types import StopCause
from autoverse.types import ToolStartPayload, TurnEndPayload, make_event
from autoverse.types.tools import ToolResult


def _loop():
    loop, _conv, _sink, app = make_loop(ScriptedProvider([]))
    return loop, app


def test_plan_mode_active_injects_section() -> None:
    loop, app = _loop()
    app.extra["plan_mode"] = {"active": True, "reason": "user toggled plan mode"}
    prompt = loop._system_prompt()  # type: ignore[attr-defined]
    assert "## Plan Mode" in prompt
    # forbids the sub-agent write bypass
    assert "sub-agent" in prompt
    # steers to the converged, numbered plan + single ExitPlanMode
    assert "ExitPlanMode" in prompt
    assert "numbered" in prompt
    assert "AskUserQuestion" in prompt
    # reason is surfaced
    assert "user toggled plan mode" in prompt


def test_plan_mode_inactive_omits_section() -> None:
    loop, app = _loop()
    # no plan_mode key at all
    assert "## Plan Mode" not in loop._system_prompt()  # type: ignore[attr-defined]
    # explicit inactive is also omitted
    app.extra["plan_mode"] = {"active": False}
    assert "## Plan Mode" not in loop._system_prompt()  # type: ignore[attr-defined]


def test_worktree_mode_active_injects_section() -> None:
    loop, app = _loop()
    app.extra["worktree_mode"] = {"active": True, "branch": "autoverse/wt/exp"}
    prompt = loop._system_prompt()  # type: ignore[attr-defined]
    assert "## Worktree Mode" in prompt
    assert "isolated git worktree" in prompt
    assert "autoverse/wt/exp" in prompt
    assert "ExitWorktreeMode" in prompt
    assert "redirected to this isolated worktree" in prompt
    assert "NOT evidence about the main work-tree" in prompt
    assert "git show main:path/to/file" in prompt


def test_worktree_mode_surfaces_live_cwd() -> None:
    """The worktree section pins the live cwd and flags it as superseding the
    (stale) Environment working directory — guards against absolute-path leaks
    outside the box.
    """
    from pathlib import Path

    loop, app = _loop()
    app.cwd = Path("/tmp/autoverse-wt-exp")
    app.extra["worktree_mode"] = {"active": True, "branch": "autoverse/wt/exp"}
    prompt = loop._system_prompt()  # type: ignore[attr-defined]
    assert "/tmp/autoverse-wt-exp" in prompt
    assert "supersedes" in prompt


def test_plan_and_worktree_mutually_exclusive_in_prompt() -> None:
    loop, app = _loop()
    # If both were somehow set, plan takes precedence and worktree is not shown.
    app.extra["plan_mode"] = {"active": True}
    app.extra["worktree_mode"] = {"active": True, "branch": "b"}
    prompt = loop._system_prompt()  # type: ignore[attr-defined]
    assert "## Plan Mode" in prompt
    assert "## Worktree Mode" not in prompt


async def test_exit_plan_mode_stops_turn_for_approval() -> None:
    async def _dispatch(calls, ctx):
        assert [c.name for c in calls] == ["ExitPlanMode"]
        ctx.app.extra["plan_mode"] = {
            "active": False,
            "pending_approval": True,
            "plan": "1. inspect\n2. edit",
        }
        return [ToolResult.ok("Plan proposed", pending_approval=True)]

    provider = ScriptedProvider(
        [
            [
                make_event(
                    ToolStartPayload(
                        tool_use_id="p1",
                        tool_name="ExitPlanMode",
                        tool_input={"plan": "1. inspect\n2. edit"},
                    )
                ),
                make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="TOOL_USE")),
            ],
            [make_event(TurnEndPayload(turn_index=0, iterations=0, stop_cause="END_TURN"))],
        ]
    )
    loop, _conv, _sink, app = make_loop(provider, dispatch=_dispatch)
    app.extra["plan_mode"] = {"active": True, "pending_approval": False}

    result = await loop.run_turn()

    assert result.cause is StopCause.PLAN_PROPOSED
    assert app.extra["plan_mode"]["pending_approval"] is True
    assert len(provider.requests) == 1
