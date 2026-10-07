"""Unit tests — PermissionRuleStore + cross-frontend rule sync (fixes #4)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.permission.matcher import PermissionRule, RuleMatcher
from autoverse.infra.permission.rule_store import PermissionRuleStore


def test_rule_store_round_trip(tmp_path: Path) -> None:
    """Rules persisted to disk are loaded back identically."""
    store = PermissionRuleStore(tmp_path)
    assert store.load() == []

    r1 = PermissionRule(tool="bash", pattern="*ls*", action="allow", source="user")
    r2 = PermissionRule(tool="delete_file", pattern="*", action="deny", source="user")
    store.add(r1)
    store.add(r2)

    loaded = store.load()
    assert len(loaded) == 2
    assert loaded[0].tool == "bash"
    assert loaded[0].action == "allow"
    assert loaded[1].tool == "delete_file"
    assert loaded[1].action == "deny"


def test_sync_rules_replaces_user_rules_preserves_config(tmp_path: Path) -> None:
    """``sync_rules`` swaps ``source='user'`` rules with the persisted set but
    keeps ``source='config'`` rules intact."""
    matcher = RuleMatcher([
        PermissionRule(tool="read_file", pattern="*", action="allow", source="config"),
        PermissionRule(tool="bash", pattern="*rm*", action="deny", source="user"),
    ])

    persisted = [
        PermissionRule(tool="bash", pattern="*ls*", action="allow", source="user"),
    ]
    matcher.sync_rules(persisted)

    rules = matcher.rules
    # Config rule preserved
    assert any(r.source == "config" and r.tool == "read_file" for r in rules)
    # Old user rule gone
    assert not any(r.pattern == "*rm*" for r in rules)
    # New persisted rule present
    assert any(r.pattern == "*ls*" and r.action == "allow" for r in rules)


def test_cross_frontend_rule_sharing(tmp_path: Path) -> None:
    """Frontend A writes a rule, Frontend B loads it from the same file."""
    store_a = PermissionRuleStore(tmp_path)
    store_b = PermissionRuleStore(tmp_path)

    matcher_a = RuleMatcher()
    matcher_b = RuleMatcher()

    # Frontend A: user picks "always allow" for bash ls
    rule = PermissionRule(tool="bash", pattern="*ls*", action="allow", source="user")
    matcher_a.add_rule(rule, prepend=True)
    store_a.add(rule)

    # Frontend B: at turn start, syncs from disk
    matcher_b.sync_rules(store_b.load())

    rules_b = matcher_b.rules
    assert any(r.tool == "bash" and r.pattern == "*ls*" and r.action == "allow" for r in rules_b)
