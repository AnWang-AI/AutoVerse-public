"""Interaction tools: AskUserQuestion.

The tool is real but UI-agnostic: frontends may expose an async callback on
``app.extra["ask_user"]``. In non-interactive contexts it returns a structured
error instead of pretending a choice was made (P5).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class AskOption(BaseModel):
    id: str = Field(..., description="Stable option id returned to the model.")
    label: str = Field(..., description="Human-readable option label.")


class AskQuestion(BaseModel):
    id: str = Field(..., description="Stable question id.")
    prompt: str = Field(..., description="Question shown to the user.")
    options: list[AskOption] = Field(..., min_length=2)
    allow_multiple: bool = Field(False)


class AskUserQuestionArgs(BaseModel):
    title: str | None = Field(None, description="Optional form title.")
    questions: list[AskQuestion] = Field(..., min_length=1)


@register_tool
class AskUserQuestionTool(BaseTool):
    name: ClassVar[str] = "AskUserQuestion"
    description: ClassVar[str] = (
        "Ask the user one or more structured multiple-choice questions. Use only "
        "when blocked on a decision that is genuinely the user's to make."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = AskUserQuestionArgs

    async def run(self, args: AskUserQuestionArgs, ctx: ToolContext) -> ToolResult:
        # Carry a compact, render-friendly summary of the questions (prompt text +
        # option id→label map) alongside the raw answer so the frontend can show a
        # readable "question → chosen labels" card without re-deriving it from the
        # tool input (ToolEndPayload exposes only output + metadata, not the args).
        questions_meta = [
            {
                "id": q.id,
                "prompt": q.prompt,
                "labels": {o.id: o.label for o in q.options},
            }
            for q in args.questions
        ]
        callback = getattr(ctx.app, "extra", {}).get("ask_user")
        if not callable(callback):
            return ToolResult.error(
                "No interactive question UI is configured. State the decision "
                "needed and pause for the user's reply.",
                error_kind="ask_user_unavailable",
                questions=questions_meta,
                title=args.title or "",
            )
        payload = args.model_dump()
        answer = await callback(payload)
        cancelled = bool(isinstance(answer, dict) and answer.get("cancelled"))
        if cancelled:
            return ToolResult.ok(
                "User dismissed the questions.\n"
                + json.dumps(answer, ensure_ascii=False, indent=2),
                answer=answer,
                questions=questions_meta,
                title=args.title or "",
                cancelled=True,
            )
        return ToolResult.ok(
            "User answered:\n" + json.dumps(answer, ensure_ascii=False, indent=2),
            answer=answer,
            questions=questions_meta,
            title=args.title or "",
        )


__all__ = [
    "AskOption",
    "AskQuestion",
    "AskUserQuestionArgs",
    "AskUserQuestionTool",
]
