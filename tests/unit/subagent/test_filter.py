"""Capability-ceiling tool filtering (docs/reference/subagents.md §5; S2/S3 unit targets)."""

from __future__ import annotations

import autoverse.core.tools.builtin  # noqa: F401 — trigger registration
from autoverse.core.subagent.definition import AgentDefinition, get_definition
from autoverse.core.subagent.filter import (
    CheckModeBashTool,
    filter_registry,
    is_tool_blocked,
)
from autoverse.core.tools.registry import ToolRegistry
from autoverse.types.capability import CapabilityTier


def _reg() -> ToolRegistry:
    return ToolRegistry.build()


def test_is_tool_blocked_ceiling() -> None:
    scout = get_definition("scout")  # READ_ONLY, no network
    assert is_tool_blocked(CapabilityTier.PROCESS, False, scout) is True
    assert is_tool_blocked(CapabilityTier.CONTROLLED_WRITE, False, scout) is True
    assert is_tool_blocked(CapabilityTier.READ_ONLY, False, scout) is False
    # network tool blocked when can_network is False
    assert is_tool_blocked(CapabilityTier.READ_ONLY, True, scout) is True


def test_is_tool_blocked_network_switch() -> None:
    researcher = get_definition("researcher")  # READ_ONLY + can_network
    assert is_tool_blocked(CapabilityTier.READ_ONLY, True, researcher) is False
    assert is_tool_blocked(CapabilityTier.PROCESS, False, researcher) is True


def test_is_tool_blocked_check_mode_hides_writes() -> None:
    validator = get_definition("validator")  # PROCESS + bash_check_mode
    assert is_tool_blocked(CapabilityTier.CONTROLLED_WRITE, False, validator) is True
    assert is_tool_blocked(CapabilityTier.ARBITRARY_WRITE, False, validator) is True
    # bash (PROCESS) is NOT blocked — it is wrapped in check_mode
    assert is_tool_blocked(CapabilityTier.PROCESS, False, validator) is False


def test_S2_scout_read_only_blocks_bash_and_network() -> None:
    sub = filter_registry(_reg(), get_definition("scout"))
    names = set(sub.names())
    assert names == {"read_file", "glob", "grep"}
    assert "bash" not in names
    assert "web_fetch" not in names and "web_search" not in names


def test_S3_validator_bash_check_mode_and_no_writes() -> None:
    sub = filter_registry(_reg(), get_definition("validator"))
    names = set(sub.names())
    assert "bash" in names
    assert isinstance(sub.get("bash"), CheckModeBashTool)
    assert not ({"write_file", "edit_file", "delete_file", "todo_write"} & names)


def test_leaf_agent_cannot_spawn() -> None:
    # craft a definition with the agent tool registered as an extra
    from autoverse.infra.tools.agent_tool import AgentTool

    class _FakeRunner:
        async def spawn(self, *a: object, **k: object) -> object:  # pragma: no cover
            raise AssertionError

    reg = ToolRegistry.build()
    reg.register("subagent", [AgentTool(_FakeRunner())])  # type: ignore[arg-type]
    assert "agent" in reg.names()
    sub = filter_registry(reg, get_definition("builder"))  # can_spawn=False
    assert "agent" not in sub.names()


def test_explicit_tools_intersect() -> None:
    adef = AgentDefinition(
        name="custom",
        description="x",
        max_tier=CapabilityTier.PROCESS,
        can_network=True,
        tools=("read_file", "grep"),
    )
    sub = filter_registry(_reg(), adef)
    assert set(sub.names()) == {"read_file", "grep"}
