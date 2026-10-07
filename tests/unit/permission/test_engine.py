"""Unit: PermissionEngine (P4, P6, P7)."""

from __future__ import annotations

import asyncio

from autoverse.infra.permission.engine import PermissionEngine
from autoverse.infra.permission.matcher import PermissionRule, RuleMatcher
from autoverse.types.tools import Decision, ToolCall


class _StubEngine(PermissionEngine):
    def __init__(self, matcher: RuleMatcher, mode: str = "ask", reply: str = "allow") -> None:
        super().__init__(matcher, global_mode=mode)  # type: ignore[arg-type]
        self._reply = reply
        self.approval_calls = 0

    async def request_approval(self, call: ToolCall) -> Decision:
        self.approval_calls += 1
        return Decision.allow() if self._reply == "allow" else Decision.deny("user denied")


async def test_p6_explicit_allow_rule_terminal() -> None:
    eng = _StubEngine(RuleMatcher([PermissionRule("think", "*", "allow")]))
    d = await eng.check(ToolCall("1", "think", {}))
    assert d.allowed and eng.approval_calls == 0


async def test_p6_explicit_deny_rule_terminal() -> None:
    eng = _StubEngine(RuleMatcher([PermissionRule("bash", "*", "deny")]))
    d = await eng.check(ToolCall("1", "bash", {"command": "ls"}))
    assert not d.allowed and eng.approval_calls == 0


async def test_p4_policy_readonly_allows_without_approval() -> None:
    eng = _StubEngine(RuleMatcher())
    d = await eng.check(ToolCall("1", "read_file", {"file_path": "/x"}))
    assert d.allowed and eng.approval_calls == 0


async def test_p4_ask_rule_routes_to_approval() -> None:
    eng = _StubEngine(RuleMatcher([PermissionRule("bash", "*", "ask")]), reply="allow")
    d = await eng.check(ToolCall("1", "bash", {"command": "ls"}))
    assert d.allowed and eng.approval_calls == 1


async def test_p4_irreversible_routes_to_approval() -> None:
    eng = _StubEngine(RuleMatcher(), reply="deny")
    d = await eng.check(ToolCall("1", "delete_file", {"path": "/x"}))
    assert not d.allowed and eng.approval_calls == 1


async def test_p7_cancel_all_denies_pending() -> None:
    eng = _StubEngine(RuleMatcher())
    fut = eng._pending.open("call-1")
    eng.cancel_all()
    d = await asyncio.wait_for(fut, timeout=1.0)
    assert not d.allowed


async def test_global_deny_mode() -> None:
    eng = _StubEngine(RuleMatcher(), mode="deny")
    d = await eng.check(ToolCall("1", "delete_file", {"path": "/x"}))
    assert not d.allowed and eng.approval_calls == 0


async def test_pending_open_uses_running_loop() -> None:
    """_PendingApprovals.open binds the future to the running loop (not the
    deprecated get_event_loop)."""
    eng = _StubEngine(RuleMatcher())
    fut = eng._pending.open("call-loop")
    assert fut.get_loop() is asyncio.get_running_loop()
    eng.cancel_all()


def test_pending_open_outside_loop_raises() -> None:
    """get_running_loop raises RuntimeError when no loop is running (was silently
    creating a deprecated loop via get_event_loop)."""
    eng = _StubEngine(RuleMatcher())
    try:
        eng._pending.open("call-noloop")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError without a running event loop")


def test_check_sync_allow_all_dangerous() -> None:
    """check_sync must NOT return allow for irreversible ops even when
    global_mode=='allow' (docs/reference/tools.md §3.2)."""
    eng = _StubEngine(RuleMatcher(), mode="allow")
    d = eng.check_sync(ToolCall("1", "delete_file", {"path": "/x"}))
    assert not d.allowed, (
        "check_sync must deny irreversible ops even in allow_all mode"
    )
    assert d.reason is not None


def test_check_sync_allow_all_safe() -> None:
    """check_sync in allow_all mode should still allow safe reversible ops."""
    eng = _StubEngine(RuleMatcher(), mode="allow")
    d = eng.check_sync(ToolCall("1", "read_file", {"file_path": "/x"}))
    assert d.allowed, "check_sync should allow reversible ops in allow_all mode"
