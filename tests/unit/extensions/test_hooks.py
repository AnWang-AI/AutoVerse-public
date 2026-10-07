"""Unit tests for HookEngine (docs/reference/hooks.md)."""

from __future__ import annotations

import asyncio
import shlex
import sys

import pytest

from autoverse.infra.hooks import HookDef, HookEngine, HookEvent, HookInput, HookOutcome
from autoverse.infra.hooks.shell import ShellHookHandler, parse_hooks_config
from autoverse.types.tools import ToolCall


def _call(name: str) -> ToolCall:
    return ToolCall(id="x", name=name, input={})


async def _deny(_: HookInput) -> HookOutcome:
    return HookOutcome(decision="deny", reason="nope")


async def _ctx(text: str) -> HookOutcome:
    return HookOutcome(additional_context=text)


async def test_pretooluse_first_deny_short_circuits() -> None:
    seen: list[str] = []

    async def record_then_proceed(inp: HookInput) -> HookOutcome:
        seen.append("first")
        return HookOutcome()

    async def deny(inp: HookInput) -> HookOutcome:
        seen.append("second")
        return HookOutcome(decision="deny", reason="blocked")

    async def never(inp: HookInput) -> HookOutcome:
        seen.append("third")
        return HookOutcome()

    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, record_then_proceed))
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, deny))
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, never))

    outcome = await engine.run(HookEvent.PRE_TOOL_USE, tool=_call("bash"))
    assert outcome.decision == "deny" and outcome.reason == "blocked"
    assert seen == ["first", "second"]  # third never runs (short-circuit)


async def test_matcher_fnmatch_filters_tools() -> None:
    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, _deny, matcher="write_*"))
    # Non-matching tool -> hook skipped -> proceed.
    assert (await engine.run(HookEvent.PRE_TOOL_USE, tool=_call("read_file"))).decision == "proceed"
    # Matching tool -> denied.
    assert (await engine.run(HookEvent.PRE_TOOL_USE, tool=_call("write_file"))).decision == "deny"


async def test_pretooluse_timeout_fails_closed() -> None:
    async def slow(_: HookInput) -> HookOutcome:
        await asyncio.sleep(3600)
        return HookOutcome()

    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, slow, timeout=0.01))
    outcome = await asyncio.wait_for(
        engine.run(HookEvent.PRE_TOOL_USE, tool=_call("bash")), timeout=2.0
    )
    assert outcome.decision == "deny"
    assert "timed out" in outcome.reason and "fail-closed" in outcome.reason


async def test_pretooluse_exception_fails_closed() -> None:
    async def boom(_: HookInput) -> HookOutcome:
        raise RuntimeError("kaboom")

    engine = HookEngine()
    engine.add(HookDef(HookEvent.PRE_TOOL_USE, boom, timeout=1.0))
    outcome = await engine.run(HookEvent.PRE_TOOL_USE, tool=_call("bash"))
    assert outcome.decision == "deny" and "fail-closed" in outcome.reason


async def test_shell_hook_nonzero_exit_fails_closed() -> None:
    script = "import sys; sys.stderr.write('broken hook'); sys.exit(7)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    engine = HookEngine()
    engine.add(
        HookDef(
            HookEvent.PRE_TOOL_USE,
            ShellHookHandler(command),
            timeout=2.0,
        )
    )

    outcome = await engine.run(HookEvent.PRE_TOOL_USE, tool=_call("bash"))

    assert outcome.decision == "deny"
    assert "hook command exited with status 7" in outcome.reason
    assert "fail-closed" in outcome.reason


async def test_post_tool_use_timeout_degrades_not_blocks() -> None:
    async def slow(_: HookInput) -> HookOutcome:
        await asyncio.sleep(3600)
        return HookOutcome()

    engine = HookEngine()
    engine.add(HookDef(HookEvent.POST_TOOL_USE, slow, timeout=0.01, blocking=False))
    # Non-blocking point: timeout degrades to a no-op outcome (does not deny).
    outcome = await asyncio.wait_for(
        engine.run(HookEvent.POST_TOOL_USE, tool=_call("bash")), timeout=2.0
    )
    assert outcome.decision == "proceed"


async def test_session_start_merges_additional_context_in_order() -> None:
    engine = HookEngine()
    engine.add(HookDef(HookEvent.SESSION_START, lambda i: _ctx("first"), blocking=False))
    engine.add(HookDef(HookEvent.SESSION_START, lambda i: _ctx("second"), blocking=False))
    outcome = await engine.run(HookEvent.SESSION_START)
    assert outcome.additional_context == "first\nsecond"


def test_prompt_fragment_projects_only_blocking_pre_hooks() -> None:
    engine = HookEngine()
    # PostToolUse (passive audit) must NOT appear in the prompt (§8.1 noise).
    engine.add(HookDef(HookEvent.POST_TOOL_USE, _ctx, blocking=False))
    assert engine.prompt_fragment() == ""
    engine.add(
        HookDef(HookEvent.PRE_TOOL_USE, _deny, matcher="file_*", description="no env writes")
    )
    frag = engine.prompt_fragment()
    assert "Active Tool Hooks" in frag and "file_*" in frag and "no env writes" in frag


def test_is_extension_source_shape() -> None:
    engine = HookEngine()
    assert engine.name == "hooks"
    assert engine.contributed_tools() == []


def test_parse_hooks_config_skips_malformed_entries(tmp_path) -> None:
    parsed = parse_hooks_config(
        {
            "Unknown": [{"command": "echo ignored"}],
            "PreToolUse": [
                "not an entry",
                {"matcher": "write_*"},
                {"command": "echo bad timeout", "timeout": "bad"},
                {"command": "echo bad blocking", "blocking": "false"},
                {"command": "echo ok", "matcher": "write_*", "timeout": 1, "blocking": False},
            ],
            "PostToolUse": "not a list",
        },
        tmp_path,
    )

    hooks = parsed[HookEvent.PRE_TOOL_USE]
    assert len(hooks) == 1
    assert hooks[0].matcher == "write_*"
    assert hooks[0].timeout == 1.0
    assert hooks[0].blocking is False
