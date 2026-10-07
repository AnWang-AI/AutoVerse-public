"""SkillTool — model-invocable skills (docs/reference/skills.md).

A single ``Skill`` tool the model calls by name; it returns the matched
Claude/Codex-style skill instructions for inline use.

The tool reads the live ``SkillRegistry`` so deleting a skill removes it from
tool availability as well as from the prompt (P1). ``infra`` only (no frontends,
P3).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.infra.skills.registry import SkillRegistry
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class SkillArgs(BaseModel):
    skill: str = Field(..., description="Name of the skill to invoke.")
    args: str = Field("", description="Optional arguments passed to the skill.")


class SkillTool(BaseTool):
    name: ClassVar[str] = "Skill"
    description: ClassVar[str] = "Invoke a named skill (reusable prompt template)."
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    Args: ClassVar[type[BaseModel]] = SkillArgs

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def run(self, args: SkillArgs, ctx: ToolContext) -> ToolResult:
        skill = self._registry.get(args.skill)
        if skill is None:
            return ToolResult.error(
                f"Unknown skill {args.skill!r}.", error_kind="unknown_skill"
            )
        rendered = skill.render(args.args)
        return ToolResult.ok(rendered, skill=skill.name, mode="inline")


__all__ = ["SkillTool", "SkillArgs"]
