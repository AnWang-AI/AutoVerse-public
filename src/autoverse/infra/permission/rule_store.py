"""PermissionRuleStore — durable, cross-process storage for "always" rules.

When a user picks "Always allow" (or "Always deny") in either the TUI or WebUI,
the scoped :class:`PermissionRule` is persisted here so the *other* frontend's
engine picks it up on its next turn — without it each process keeps its own
in-memory ``RuleMatcher`` and the rule never crosses the process boundary.

Layout: ``<data_dir>/permissions.jsonl`` — one JSON object per line::

    {"tool": "bash", "pattern": "*ls*", "action": "allow", "source": "user"}

The store is intentionally simple (append-only JSONL + full reload). ``load()``
reads all lines; ``add()`` appends one. Dedup is the caller's job (a reload +
match before persisting avoids exact-duplicates, but the matcher's first-match
semantics make duplicates harmless).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from autoverse.infra.permission.matcher import PermissionRule


class PermissionRuleStore:
    """JSONL-backed persistence for user-scoped permission rules."""

    def __init__(self, data_dir: Path | str) -> None:
        self._path = Path(data_dir) / "permissions.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> "list[PermissionRule]":
        """Load all persisted rules from disk (best-effort: skips bad lines)."""
        from autoverse.infra.permission.matcher import PermissionRule

        if not self._path.exists():
            return []
        rules: list[PermissionRule] = []
        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                rules.append(
                    PermissionRule(
                        tool=str(obj.get("tool", "*")),
                        pattern=str(obj.get("pattern", "*")),
                        action=str(obj.get("action", "ask")),  # type: ignore[arg-type]
                        source=str(obj.get("source", "user")),
                    )
                )
        return rules

    def add(self, rule: "PermissionRule") -> None:
        """Append one rule to the JSONL file (durable immediately)."""
        line = (
            json.dumps(
                {
                    "tool": rule.tool,
                    "pattern": rule.pattern,
                    "action": rule.action,
                    "source": rule.source,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)


__all__ = ["PermissionRuleStore"]
