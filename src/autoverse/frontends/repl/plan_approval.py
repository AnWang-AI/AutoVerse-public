"""Shared helpers for plan approval flows in REPL frontends."""

from __future__ import annotations


def approved_plan_prompt(plan: str) -> str:
    """Prompt used to continue automatically after the user approves a plan."""
    body = plan.strip()
    if body:
        return (
            "The user approved the following implementation plan. You are now in "
            "normal mode. Execute the plan and make the required changes. Do not "
            "re-enter plan mode unless the approved plan is impossible to execute "
            "without a new user decision.\n\n"
            "Approved plan:\n"
            f"{body}"
        )
    return (
        "The user approved the proposed implementation plan. You are now in "
        "normal mode. Execute the approved plan and make the required changes."
    )


__all__ = ["approved_plan_prompt"]
