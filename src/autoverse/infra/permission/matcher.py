"""Permission rule matcher (docs/reference/tools.md). First-match-wins fnmatch rules.

A PermissionRule matches a ToolCall when the tool glob matches the call name AND
the pattern glob matches `json.dumps(call.input)`. Rules are evaluated in order;
the first match wins (P5/P8). `make_scoped_rule` derives a narrowed rule from a
call+action for "always allow/deny this kind" — for bash it narrows by the first
command token, never producing a catch-all.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal

from autoverse.types.tools import ToolCall

RuleAction = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class PermissionRule:
    tool: str = "*"
    pattern: str = "*"
    action: RuleAction = "ask"
    source: str = "config"


def _input_repr(call: ToolCall) -> str:
    try:
        return json.dumps(call.input, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(call.input)


class RuleMatcher:
    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self._rules: list[PermissionRule] = list(rules or [])

    @property
    def rules(self) -> list[PermissionRule]:
        return list(self._rules)

    def add_rule(self, rule: PermissionRule, prepend: bool = True) -> None:
        """Add a rule. Prepend (default) so newer/scoped rules win first-match."""
        if prepend:
            self._rules.insert(0, rule)
        else:
            self._rules.append(rule)

    def sync_rules(self, persisted: list[PermissionRule]) -> None:
        """Replace all ``source='user'`` rules with the persisted set.

        Called at turn start (after ``sync_from_store``) so "always allow" rules
        another frontend wrote to disk take effect in this process too. Rules
        from ``source='config'`` (built-in / config-file rules) are preserved
        untouched; the persisted ``source='user'`` rules are prepended so they
        keep first-match priority."""
        config_rules = [r for r in self._rules if r.source != "user"]
        # Persisted rules go first (first-match wins), then the config rules.
        self._rules = list(persisted) + config_rules

    def match_explained(
        self, call: ToolCall
    ) -> tuple[RuleAction | None, PermissionRule | None]:
        """Return the first matching (action, rule), or (None, None)."""
        blob = _input_repr(call)
        for rule in self._rules:
            if fnmatch(call.name, rule.tool) and fnmatch(blob, rule.pattern):
                return (rule.action, rule)
        return (None, None)


def make_scoped_rule(call: ToolCall, action: RuleAction) -> PermissionRule:
    """Derive a narrowed rule from a call+action (never a catch-all)."""
    if call.name == "bash":
        cmd = str(call.input.get("command", ""))
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()
        first = tokens[0] if tokens else ""
        pattern = f"*{first}*" if first else "*"
        return PermissionRule(
            tool="bash", pattern=pattern, action=action, source="user"
        )
    return PermissionRule(tool=call.name, pattern="*", action=action, source="user")


def explain(action: RuleAction | None, rule: PermissionRule | None) -> str:
    """Human-readable explanation of a match outcome."""
    if rule is None or action is None:
        return "no matching rule"
    return f"rule[{rule.source}] {rule.tool}/{rule.pattern} -> {action}"


__all__ = [
    "RuleAction",
    "PermissionRule",
    "RuleMatcher",
    "make_scoped_rule",
    "explain",
]
