"""Unit: RuleMatcher (P5, P8)."""

from __future__ import annotations

from autoverse.infra.permission.matcher import (
    PermissionRule,
    RuleMatcher,
    make_scoped_rule,
)
from autoverse.types.tools import ToolCall


def test_p5_first_match_wins() -> None:
    m = RuleMatcher(
        [
            PermissionRule(tool="*_file", pattern="*", action="allow"),
            PermissionRule(tool="write_file", pattern="*", action="deny"),
        ]
    )
    action, rule = m.match_explained(ToolCall("1", "write_file", {}))
    assert action == "allow"  # first rule wins


def test_p8_pattern_matches_input_blob() -> None:
    m = RuleMatcher(
        [PermissionRule(tool="bash", pattern="*danger*", action="deny")]
    )
    action, _ = m.match_explained(ToolCall("1", "bash", {"command": "danger"}))
    assert action == "deny"
    action2, _ = m.match_explained(ToolCall("2", "bash", {"command": "safe"}))
    assert action2 is None


def test_add_rule_prepend_overrides() -> None:
    m = RuleMatcher([PermissionRule(tool="*", pattern="*", action="deny")])
    m.add_rule(PermissionRule(tool="think", pattern="*", action="allow"), prepend=True)
    action, _ = m.match_explained(ToolCall("1", "think", {}))
    assert action == "allow"


def test_make_scoped_rule_bash_narrows_by_token() -> None:
    rule = make_scoped_rule(ToolCall("1", "bash", {"command": "npm install x"}), "allow")
    assert rule.tool == "bash"
    assert "npm" in rule.pattern
    assert rule.pattern != "*"  # never a catch-all


def test_make_scoped_rule_other_tools() -> None:
    rule = make_scoped_rule(ToolCall("1", "read_file", {}), "deny")
    assert rule.tool == "read_file" and rule.action == "deny"
