"""Plan-mode tools.

Plan mode is an explicit state on ``AppContext.extra``. ``ExitPlanMode`` submits
a proposed plan for user approval; it does not grant implementation permission.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class EnterPlanModeArgs(BaseModel):
    reason: str = Field(..., description="Why planning mode is needed.")


class ExitPlanModeArgs(BaseModel):
    plan: str = Field(
        ...,
        description=(
            "The proposed implementation plan: a converged, numbered list of "
            "concrete steps (no unresolved 'Option A/B' alternatives)."
        ),
    )


@register_tool
class EnterPlanModeTool(BaseTool):
    name: ClassVar[str] = "EnterPlanMode"
    description: ClassVar[str] = (
        "Enter planning mode before implementation. Use for broad, risky, or "
        "ambiguous changes that require user approval of a plan. Once in plan "
        "mode, stay read-only (research/read only — write tools are blocked), "
        "produce a numbered implementation plan, and call ExitPlanMode to submit "
        "it for approval. Do not repeatedly attempt write operations while in "
        "plan mode."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = EnterPlanModeArgs

    async def run(self, args: EnterPlanModeArgs, ctx: ToolContext) -> ToolResult:
        existing = ctx.app.extra.get("plan_mode")
        if isinstance(existing, dict) and existing.get("active") is True:
            return ToolResult.ok(
                "Already in plan mode. Continue read-only research and call "
                "ExitPlanMode once with a converged, numbered plan. Do not call "
                "EnterPlanMode again.",
                active=True,
                pending_approval=False,
                already_active=True,
            )
        ctx.app.extra["plan_mode"] = {
            "active": True,
            "reason": args.reason,
            "pending_approval": False,
        }
        return ToolResult.ok(
            "Entered plan mode. Produce a concise plan and call ExitPlanMode "
            "when ready for user approval.",
            active=True,
            pending_approval=False,
        )


@register_tool
class ExitPlanModeTool(BaseTool):
    name: ClassVar[str] = "ExitPlanMode"
    description: ClassVar[str] = (
        "Exit planning mode with the proposed plan. This does not implement the "
        "plan; it surfaces the plan for approval."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = ExitPlanModeArgs

    async def run(self, args: ExitPlanModeArgs, ctx: ToolContext) -> ToolResult:
        plan = args.plan.strip()
        if not plan:
            return ToolResult.error(
                "ExitPlanMode requires a non-empty numbered implementation plan.",
                error_kind="invalid_plan",
            )
        ctx.app.extra["plan_mode"] = {
            "active": False,
            "pending_approval": True,
            "plan": plan,
        }
        return ToolResult.ok(
            "Plan proposed; waiting for user approval before implementation:\n" + plan,
            active=False,
            pending_approval=True,
            plan=plan,
        )


__all__ = [
    "EnterPlanModeArgs",
    "EnterPlanModeTool",
    "ExitPlanModeArgs",
    "ExitPlanModeTool",
]
