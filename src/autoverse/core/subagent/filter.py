"""Three-step tool filtering + controlled bash (docs/reference/subagents.md §5.1/§5.2/§5.3).

Capability ceiling (``max_tier``) + orthogonal network switch (``can_network``)
replace the legacy allowed/blocked double list (P7). The visibility formula
(docs/architecture/contracts.md §4.1):

    visible ⟺  tool.tier <= max_tier  AND  (not tool.needs_network or can_network)

``bash_check_mode`` agents (validator/reviewer) keep bash but it is wrapped in
:class:`CheckModeBashTool`, which forces ``check_mode=True`` before delegating to
the real :class:`BashTool` — so bash can run read-only checks but never writes.
Write-tier tools are additionally hidden. This avoids modifying the Phase-2
``BashTool`` body.

``core`` purity: imports only ``types``/sibling ``core`` modules + stdlib.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from autoverse.core.subagent.definition import AgentDefinition
from autoverse.core.tools.base import BaseTool, Tool
from autoverse.core.tools.builtin.shell import (
    BashArgs,
    BashTool,
    is_readonly_command,
    is_trusted_check_command,
)
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import ToolRegistry
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult

# bash_check_mode additionally blocks these "write" filesystem tiers (bash is NOT
# blocked — it is wrapped in check_mode instead, §5.3).
_WRITE_TIERS: frozenset[CapabilityTier] = frozenset(
    {CapabilityTier.CONTROLLED_WRITE, CapabilityTier.ARBITRARY_WRITE}
)

# Built-in tool names known to this design (MCP / unregistered tools fail-closed).
_KNOWN_BUILTINS: frozenset[str] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "glob",
        "grep",
        "bash",
        "web_fetch",
        "web_search",
        "think",
        "tool_search",
        "notebook_edit",
        "todo_write",
        "agent",
        "browser_navigate",
        "browser_list_pages",
        "browser_new_page",
        "browser_switch_page",
        "browser_close_page",
        "browser_screenshot",
        "browser_click",
        "browser_type",
        "browser_evaluate",
        "browser_get_content",
        "browser_scroll",
        "browser_close",
        "browser_wait",
        "browser_select",
        "browser_keyboard",
        "generate_image",
        "show_image",
    }
)

# General bans (orthogonal to the capability ceiling, §5.2 step 1). Session-level
# interaction-mode tools (plan/worktree) must not be reachable from a sub-agent:
# a child must not flip the parent session's mode or worktree state.
_PLAN_MODE_TOOLS: frozenset[str] = frozenset(
    {"EnterPlanMode", "ExitPlanMode", "EnterWorktreeMode", "ExitWorktreeMode"}
)


def is_tool_blocked(
    tool_cap: CapabilityTier,
    tool_needs_network: bool,
    adef: AgentDefinition,
) -> bool:
    """Capability ceiling hard boundary (single source of truth, §5.1).

    Returns True (blocked) when the tool exceeds the filesystem ceiling, needs
    network the agent was not granted, or is a write-tier tool under
    bash_check_mode.
    """
    if tool_cap > adef.max_tier:
        return True
    if tool_needs_network and not adef.can_network:
        return True
    if adef.bash_check_mode and tool_cap in _WRITE_TIERS:
        return True
    return False


class CheckModeBashTool(BaseTool):
    """Thin wrapper around :class:`BashTool` that forces check_mode (§5.3).

    Used for ``bash_check_mode`` agents so bash runs read-only checks (tests,
    lint) but state-modifying commands are rejected. The Phase-2 ``BashTool``
    body is unchanged; we set ``args.check_mode=True`` and delegate.
    """

    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "Execute a bash command in check_mode (read-only; rejects "
        "state-modifying commands)."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.PROCESS
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = BashArgs

    def __init__(self) -> None:
        self._inner = BashTool()

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BashArgs)
        if is_trusted_check_command(args.command) and not is_readonly_command(args.command):
            # Trusted verification commands (pytest/lint/build/typecheck) are the
            # validator's job. Execute them for real inside the current cwd /
            # worktree, while arbitrary writes remain blocked by check_mode.
            real = args.model_copy(update={"check_mode": False})
            return await self._inner.run(real, ctx)
        forced = args.model_copy(update={"check_mode": True})
        return await self._inner.run(forced, ctx)


def filter_registry(registry: ToolRegistry, adef: AgentDefinition) -> ToolRegistry:
    """Three-step filtering with runtime enforcement (the core safety guarantee).

    Builds a NEW :class:`ToolRegistry` containing only the visible subset. For
    ``bash_check_mode`` agents the ``bash`` entry is the :class:`CheckModeBashTool`
    wrapper. Non-builtin runtime tools (e.g. ``agent``) that survive filtering
    are re-attached via ``extra=``.
    """
    names = set(registry.names())

    # Step 1: general bans (orthogonal to the ceiling).
    if not adef.can_spawn:
        names.discard("agent")
    names -= _PLAN_MODE_TOOLS

    # Step 2: capability ceiling hard boundary (runtime fallback even if tools is wrong).
    kept: set[str] = set()
    for n in names:
        tool = registry.get(n)
        if tool is None:
            continue
        if not is_tool_blocked(tool.capability, tool.needs_network, adef):
            kept.add(n)
    names = kept

    # Step 3: MCP / unregistered tools fail-closed — unknown is rejected.
    allow = adef.tools or ()
    names = {n for n in names if n in _KNOWN_BUILTINS or n in allow}

    # Step 4: if tools is explicitly declared, intersect.
    if adef.tools is not None:
        names &= set(adef.tools)

    # Build the subset. Builtins are re-instantiated by name; surviving non-builtin
    # runtime tools are carried over as `extra`, with bash possibly swapped for the
    # check_mode wrapper.
    builtin_names = {n for n in names if _is_declared_builtin(n)}
    extras: list[Tool] = []
    for n in names - builtin_names:
        tool = registry.get(n)
        if tool is not None:
            extras.append(tool)

    if adef.bash_check_mode and "bash" in names:
        builtin_names.discard("bash")
        extras.append(CheckModeBashTool())

    return ToolRegistry.build(include=builtin_names, extra=tuple(extras))


def _is_declared_builtin(name: str) -> bool:
    """True if ``name`` is in the builtin declaration table (re-buildable by build())."""
    from autoverse.core.tools.registry import _REGISTRY_DECL

    return any(getattr(t, "name", None) == name for t in _REGISTRY_DECL)


__all__ = [
    "is_tool_blocked",
    "CheckModeBashTool",
    "filter_registry",
]
