"""Unit: permission policy (P1, P2, P3, P9)."""

from __future__ import annotations

from pathlib import Path

from autoverse.infra.permission.policy import ToolMeta, assess_call
from autoverse.types.tools import ToolCall


def test_p1_irreversible_delete_asks() -> None:
    a = assess_call(ToolCall("1", "delete_file", {"path": "/x"}), ToolMeta())
    assert a.needs_ask is True and "irreversible" in a.reason


def test_p1_destructive_bash_asks() -> None:
    a = assess_call(ToolCall("1", "bash", {"command": "rm -rf /"}), ToolMeta())
    assert a.needs_ask is True


def test_p2_hidden_cost_asks() -> None:
    # Hidden cost (paid / externally-visible) -> ask. No paid builtin exists
    # yet, so model one via a ToolMeta subclass; this verifies the Q2 branch
    # without conflating network-read with hidden cost (docs/reference/tools.md §5 P3).
    class PaidMeta(ToolMeta):
        def hidden_cost(self, call: ToolCall) -> bool:
            return call.name == "generate_image"

        def hidden_cost_kind(self, call: ToolCall) -> str:
            return "paid_generation"

    a = assess_call(ToolCall("1", "generate_image", {"prompt": "x"}), PaidMeta())
    assert a.needs_ask is True and "hidden cost" in a.reason


def test_p2_network_read_allows() -> None:
    # Plain network reads are reversible + no hidden cost -> auto-allow
    # (docs/reference/tools.md §5 P1; not the approval-fatigue anti-pattern KD-1 warns of).
    for name, inp in (("web_search", {"query": "x"}), ("web_fetch", {"url": "http://x"})):
        a = assess_call(ToolCall("1", name, inp), ToolMeta())
        assert a.needs_ask is False


def test_p3_readonly_allows() -> None:
    a = assess_call(ToolCall("1", "read_file", {"file_path": "/x"}), ToolMeta())
    assert a.needs_ask is False


def test_p3_reversible_write_allows() -> None:
    a = assess_call(ToolCall("1", "write_file", {"file_path": "/x"}), ToolMeta())
    assert a.needs_ask is False


def test_file_write_new_file_allows(tmp_path: Path) -> None:
    # Target does not exist -> creating a new file is reversible -> allow.
    target = tmp_path / "new.txt"
    a = assess_call(
        ToolCall("1", "write_file", {"file_path": str(target)}), ToolMeta()
    )
    assert a.needs_ask is False


def test_file_edit_existing_allows(tmp_path: Path) -> None:
    # edit_file is reversible: old_string/new_string both in call args, swap
    # back to restore. No permission needed even on an existing file.
    target = tmp_path / "exists.txt"
    target.write_text("original")
    a = assess_call(
        ToolCall(
            "1",
            "edit_file",
            {"file_path": str(target), "old_string": "o", "new_string": "x"},
        ),
        ToolMeta(),
    )
    assert a.needs_ask is False


def test_unknown_tool_irreversible() -> None:
    """Unknown tools (e.g. MCP-injected) must be treated as irreversible
    (fail-safe / fail-closed per ADR-0008 and §9.4)."""
    meta = ToolMeta()
    # An unknown tool name that is not in any known reversible/irreversible set.
    a = assess_call(ToolCall("1", "mcp_injected_danger", {}), meta)
    assert a.needs_ask is True, (
        "Unknown tool must be treated as irreversible (fail-safe)"
    )
    assert "irreversible" in a.reason


def test_p9_policy_source_contains_no_effect_type() -> None:
    # The decision tree must rely only on reversibility/hidden_cost (P9).
    src = (
        Path(__file__).resolve().parents[3]
        / "src/autoverse/infra/permission/policy.py"
    ).read_text()
    assert "effect_type" not in src


def test_known_reversible_tools_stay_reversible() -> None:
    """Sanity: all known reversible tools must still return needs_ask=False."""
    meta = ToolMeta()
    reversible = [
        "read_file", "glob", "grep", "edit_file",
        "web_search", "web_fetch",
        "think", "todo_write", "tool_search", "notebook_edit",
        "browser_navigate", "browser_screenshot", "browser_get_content",
        "generate_image", "read_image", "generate_uuid", "hash_text", "sleep",
        "memory_read",
        "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
        "EnterWorktreeMode", "ExitWorktreeMode",
        "SessionStart", "JobStart",
    ]
    for name in reversible:
        a = assess_call(ToolCall("1", name, {}), meta)
        assert a.needs_ask is False, f"{name} should be reversible"
