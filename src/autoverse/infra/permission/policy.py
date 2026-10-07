"""Permission policy decision tree (docs/reference/tools.md).

`assess_call` decides whether a call needs human approval based purely on its
*reversibility* and *hidden cost* — it deliberately NEVER consults the call's
side-effect category field (P9; a single test asserts this source file contains
no such literal). Missing metadata is treated conservatively (lean toward ask).
"""

from __future__ import annotations
from dataclasses import dataclass

from autoverse.core.tools.builtin.shell import classify_command
from autoverse.types.tools import ToolCall

# Risk labels from shell.classify_command that are irreversible for bash.
_IRREVERSIBLE_BASH_KINDS = frozenset(
    {
        "destructive_delete",
        "raw_disk_write",
        "format_disk",
        "drop_table",
        "drop_database",
        "force_push",
        "hard_reset",
        "kill_process",
    }
)

# Known reversible tools — every tool in this set is safe (no irreversible
# side effects, no hidden cost).  Tools NOT in this set (and not explicitly
# handled as conditionally-irreversible: bash / delete_file) are treated as
# irreversible by default (fail-closed per ADR-0008
# and docs/reference/tools.md §9.4).  Add new reversible builtins here as they land.
_KNOWN_REVERSIBLE = frozenset(
    {
        # ── file / search reads ──
        "read_file",
        "glob",
        "grep",
        # ── file editing ──
        "edit_file",
        "write_file",
        # ── network reads ──
        "web_search",
        "web_fetch",
        # ── meta / scratchpad ──
        "think",
        "todo_write",
        "tool_search",
        # ── notebook (read/edit cells — edit is reversible by re-editing) ──
        "notebook_edit",
        # ── browser tools ──
        "browser_navigate",
        "browser_screenshot",
        "browser_get_content",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_wait",
        "browser_select",
        "browser_keyboard",
        "browser_evaluate",
        "browser_close",
        # ── generative / vision ──
        "generate_image",
        "read_image",
        # ── utilities ──
        "generate_uuid",
        "hash_text",
        "sleep",
        # ── memory ──
        "memory_read",
        "memory_write",
        # ── mode switching ──
        "AskUserQuestion",
        "EnterPlanMode",
        "ExitPlanMode",
        "EnterWorktreeMode",
        "ExitWorktreeMode",
        # ── framework / infra tools ──
        "agent",
        "JobOutput",
        "JobList",
        "JobGet",
        "JobStop",
        "plugins",
        "list_mcp_resources",
        "read_mcp_resource",
        "mcp",
        "hooks",
        "skills",
        "Skill",
        # ── session management ──
        "SessionSpawn",
        "SessionTell",
        "SendMessage",
        "SessionInbox",
        "SessionList",
        "SessionAbort",
        "SessionTaskUpdate",
        # ── prefix-matched entries are handled in _is_known_reversible() ──
    }
)


def _is_known_reversible(name: str) -> bool:
    """True when *name* is in the known-reversible whitelist or matches a
    recognised prefix pattern (e.g. ``Session*``, ``Job*``)."""
    if name in _KNOWN_REVERSIBLE:
        return True
    if name.startswith("Session") or name.startswith("Job"):
        return True
    if name.startswith("browser_"):
        return True
    return False


@dataclass(frozen=True)
class Assessment:
    needs_ask: bool
    reason: str = ""


class ToolMeta:
    """Reversibility / hidden-cost metadata for a tool, keyed by call.

    Defaults are conservative; bash judgments reuse shell.classify_command. This
    base class can be subclassed per-tool, but the default heuristics cover the
    builtin set for this phase.
    """

    def is_irreversible(self, call: ToolCall) -> bool:
        if call.name == "bash":
            kind, _ = classify_command(str(call.input.get("command", "")))
            return kind in _IRREVERSIBLE_BASH_KINDS
        if call.name == "delete_file":
            return True
        # Unknown tools (e.g. MCP-injected) → conservative: assume irreversible.
        # Known reversible tools (whitelist) are safe.
        return not _is_known_reversible(call.name)

    def irreversible_kind(self, call: ToolCall) -> str:
        if call.name == "bash":
            kind, _ = classify_command(str(call.input.get("command", "")))
            return kind
        if call.name == "delete_file":
            return "delete"
        return ""

    def hidden_cost(self, call: ToolCall) -> bool:
        # Hidden cost = spends money / externally visible / mutates shared state
        # (docs/reference/tools.md §5.2, docs/reference/tools.md §5 P3). Plain network *reads*
        # (web_fetch/web_search) are reversible and have NO hidden cost — they
        # must auto-allow (docs/reference/tools.md §5 P1; conflating network-read with hidden
        # cost is exactly the approval-fatigue anti-pattern KD-1 warns against).
        # Paid/externally-visible tools (e.g. generate_image, IM/PR posting) are
        # added here when they land in later phases.
        return False

    def hidden_cost_kind(self, call: ToolCall) -> str:
        return ""


def assess_call(call: ToolCall, meta: ToolMeta) -> Assessment:
    """Decide allow vs ask from reversibility (Q1) and hidden cost (Q2) only."""
    # Q1 — irreversible side effects (delete/overwrite/force-push/drop/kill).
    if meta.is_irreversible(call):
        return Assessment(
            needs_ask=True,
            reason=f"irreversible action: {meta.irreversible_kind(call) or 'unknown'}",
        )
    # Q2 — hidden cost (money / externally visible).
    if meta.hidden_cost(call):
        return Assessment(
            needs_ask=True,
            reason=f"hidden cost: {meta.hidden_cost_kind(call) or 'unknown'}",
        )
    return Assessment(needs_ask=False, reason="reversible, no hidden cost")


__all__ = ["Assessment", "ToolMeta", "assess_call"]
