"""Acceptance: permission engine P1-P9 (docs/reference/tools.md)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.permission.engine import PermissionEngine
from autoverse.infra.permission.matcher import (
    PermissionRule,
    RuleMatcher,
    make_scoped_rule,
)
from autoverse.infra.permission.policy import ToolMeta, assess_call
from autoverse.types.tools import Decision, ToolCall


class _Eng(PermissionEngine):
    def __init__(self, matcher: RuleMatcher, mode: str = "ask", reply: str = "deny") -> None:
        super().__init__(matcher, global_mode=mode)  # type: ignore[arg-type]
        self._reply = reply
        self.asks = 0

    async def request_approval(self, call: ToolCall) -> Decision:
        self.asks += 1
        return Decision.allow() if self._reply == "allow" else Decision.deny("user")


# P1: irreversible actions require approval.
async def test_p1_irreversible_requires_approval() -> None:
    eng = _Eng(RuleMatcher(), reply="deny")
    d = await eng.check(ToolCall("1", "delete_file", {"path": "/x"}))
    assert not d.allowed and eng.asks == 1


# P2: tools never call permissions — gating is centralised (only allow/deny escape).
async def test_p2_only_allow_or_deny_escape() -> None:
    eng = _Eng(RuleMatcher([PermissionRule("bash", "*", "ask")]), reply="allow")
    d = await eng.check(ToolCall("1", "bash", {"command": "ls"}))
    assert d.action in ("allow", "deny")


# P3: read-only is auto-allowed without approval.
async def test_p3_readonly_auto_allow() -> None:
    eng = _Eng(RuleMatcher())
    d = await eng.check(ToolCall("1", "read_file", {"file_path": "/x"}))
    assert d.allowed and eng.asks == 0


async def test_write_file_auto_allows_existing_target(tmp_path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old", encoding="utf-8")
    eng = _Eng(RuleMatcher())

    d = await eng.check(
        ToolCall("1", "write_file", {"file_path": str(target), "content": "new"})
    )

    assert d.allowed and eng.asks == 0


# P1 (docs/reference/tools.md §5): reversible-safe incl. network reads auto-allow.
async def test_p1_network_read_auto_allow() -> None:
    eng = _Eng(RuleMatcher())
    for name, inp in (("web_fetch", {"url": "http://x"}), ("web_search", {"query": "x"})):
        d = await eng.check(ToolCall("1", name, inp))
        assert d.allowed and eng.asks == 0


# P4: no global singletons — engine is instance-scoped; ask routes to approval.
async def test_p4_instance_scoped_and_ask_routes() -> None:
    eng = _Eng(RuleMatcher(), reply="allow")
    # delete_file is irreversible -> routes through ask -> approval reply.
    d = await eng.check(ToolCall("1", "delete_file", {"path": "/x"}))
    assert d.allowed and eng.asks == 1


# P5: deny carries a reason; first-match-wins.
async def test_p5_deny_reason_and_first_match() -> None:
    eng = _Eng(RuleMatcher([PermissionRule("*", "*", "deny")]))
    d = await eng.check(ToolCall("1", "think", {}))
    assert not d.allowed and d.reason


# P6: explicit allow/deny rules are terminal.
async def test_p6_explicit_rules_terminal() -> None:
    eng = _Eng(RuleMatcher([PermissionRule("think", "*", "allow")]))
    d = await eng.check(ToolCall("1", "think", {}))
    assert d.allowed and eng.asks == 0


# P7: cancel_all denies all pending approvals.
async def test_p7_cancel_all() -> None:
    import asyncio

    eng = _Eng(RuleMatcher())
    fut = eng._pending.open("c1")
    eng.cancel_all()
    d = await asyncio.wait_for(fut, timeout=1.0)
    assert not d.allowed


# P8: scoped rules narrow (bash by first token, never catch-all).
def test_p8_scoped_rule_narrows() -> None:
    rule = make_scoped_rule(ToolCall("1", "bash", {"command": "npm test"}), "allow")
    assert rule.pattern != "*" and "npm" in rule.pattern


# P9: policy decision tree never reads effect_type.
def test_p9_no_effect_type_in_policy() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src/autoverse/infra/permission/policy.py"
    ).read_text()
    assert "effect_type" not in src
    # And the decision still works using reversibility/hidden_cost.
    assert assess_call(ToolCall("1", "delete_file", {"path": "/x"}), ToolMeta()).needs_ask
