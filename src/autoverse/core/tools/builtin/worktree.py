"""Worktree-mode tools (docs/reference/subagents.md §5a, docs/guides/using-cli-and-tui.md §3.10).

``EnterWorktreeMode`` / ``ExitWorktreeMode`` let the model switch the session
into (and out of) the isolated-write interaction mode — the third state next to
``normal`` and ``plan``. They are thin shells over the shared orchestration in
``core/loop/worktree_mode.py`` (P1 single source), so the build/teardown logic is
identical whether the user (Tab / ``/worktree``) or the model triggers it.

Both tools are ``READ_ONLY`` (they do not themselves touch files — they flip a
session flag and ask the Isolator to build/finalize the box), mirroring the
plan-mode tools.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from autoverse.core.loop import worktree_mode
from autoverse.core.tools.base import BaseTool
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.registry import register_tool
from autoverse.types.capability import CapabilityTier
from autoverse.types.tools import ToolResult


class EnterWorktreeModeArgs(BaseModel):
    reason: str = Field(
        "",
        description="Why an isolated worktree is needed (e.g. risky refactor).",
    )
    path: str | None = Field(
        None,
        description=(
            "Optional directory/repository to enter from. Relative paths resolve "
            "against the current session cwd; useful for nested git repositories."
        ),
    )
    base_ref: str = Field("HEAD", description="Commit/ref to branch the worktree from.")
    reuse: bool = Field(
        True,
        description=(
            "Reuse this session's kept worktree when available. Set false to "
            "force a fresh worktree for demos or clean experiments."
        ),
    )


class ExitWorktreeModeArgs(BaseModel):
    action: str = Field(
        "",
        description=(
            "How to finalize the worktree on exit: 'keep' (keep branch+changes "
            "for manual merge/PR — you must git commit first, otherwise it "
            "returns an error), 'merge' (commit and attempt merge back to base; "
            "fail-safe on conflict), or 'discard' (drop changes and remove the "
            "worktree). Omit only for an automatic clean-exit: a pristine worktree "
            "is discarded; a changed/unknown worktree requires an explicit action."
        ),
    )


@register_tool
class EnterWorktreeModeTool(BaseTool):
    name: ClassVar[str] = "EnterWorktreeMode"
    description: ClassVar[str] = (
        "Switch the session into isolated worktree mode: subsequent edits and "
        "commands run inside a fresh git worktree on a new branch, leaving the "
        "main work-tree untouched. Use for risky/experimental changes you want "
        "to review before merging. Call ExitWorktreeMode to leave and decide "
        "keep/merge/discard. Requires a git repository (fails closed otherwise)."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = EnterWorktreeModeArgs

    async def run(self, args: EnterWorktreeModeArgs, ctx: ToolContext) -> ToolResult:
        res = await worktree_mode.enter(
            ctx.app,
            reason=args.reason,
            base_ref=args.base_ref,
            reuse=args.reuse,
            base_path=args.path,
        )
        if not res.ok:
            return ToolResult.error(
                f"Could not enter worktree mode: {res.error}",
                error_kind="worktree_enter_failed",
            )
        # Reuse resumes this session's existing box (a prior 'keep' exit) rather
        # than minting a new one — say so, so the model knows prior work is here.
        verb = "Resumed" if res.reused else "Entered"
        orphan_note = (
            f" Detected {res.orphan_count} untracked AutoVerse worktree directory(s); "
            "review cleanup before they accumulate."
            if res.orphan_count
            else ""
        )
        reuse_note = ""
        if res.reused:
            state = (
                "has changes"
                if res.has_changes is True
                else "appears clean"
                if res.has_changes is False
                else "change status unknown"
            )
            reuse_note = f" Reused an existing kept worktree ({state}); pass reuse=false for a fresh one."
        return ToolResult.ok(
            f"{verb} worktree mode on branch {res.branch}. Edits and commands now "
            f"run in {res.cwd}, isolated from the main work-tree. Call "
            "ExitWorktreeMode with action='keep', action='merge', or "
            "action='discard' when there is work to preserve. Omitting action "
            "only auto-discards a pristine worktree."
            + reuse_note
            + orphan_note,
            active=True,
            branch=res.branch,
            cwd=str(res.cwd) if res.cwd else None,
            reused=res.reused,
            has_changes=res.has_changes,
            orphan_count=res.orphan_count,
        )


@register_tool
class ExitWorktreeModeTool(BaseTool):
    name: ClassVar[str] = "ExitWorktreeMode"
    description: ClassVar[str] = (
        "Leave isolated worktree mode and finalize the changes. Commit your work "
        "(git add + git commit) BEFORE calling this. Pass action='keep' to keep the "
        "branch for manual merge/PR, 'merge' to commit + merge back to base, or "
        "'discard' to drop the worktree. Uncommitted changes will cause 'keep' to "
        "return an error — commit first, then retry. If action is omitted, only a "
        "pristine worktree is auto-discarded."
    )
    capability: ClassVar[CapabilityTier] = CapabilityTier.READ_ONLY
    needs_network: ClassVar[bool] = False
    Args: ClassVar[type[BaseModel]] = ExitWorktreeModeArgs

    async def run(self, args: ExitWorktreeModeArgs, ctx: ToolContext) -> ToolResult:
        action = (args.action or "").strip()
        if not action or action == "auto":
            if not worktree_mode.is_active(ctx.app):
                action = "discard"
            else:
                dirty = await worktree_mode.has_changes(ctx.app)
                if dirty is not False:
                    state = "has changes" if dirty is True else "could not be checked"
                    return ToolResult.error(
                        "ExitWorktreeMode needs an explicit action because the "
                        f"worktree {state}. Re-run with action='keep', "
                        "action='merge', or action='discard'.",
                        error_kind="worktree_exit_action_required",
                        active=True,
                        has_changes=dirty,
                        needs_action=True,
                    )
                action = "discard"
        elif action not in {"keep", "merge", "discard"}:
            return ToolResult.error(
                "ExitWorktreeMode action must be one of: keep, merge, discard.",
                error_kind="worktree_exit_invalid_action",
                active=worktree_mode.is_active(ctx.app),
                action=action,
            )

        res = await worktree_mode.exit(ctx.app, action=action)
        if not res.ok:
            return ToolResult.error(
                f"Exit worktree mode reported an issue: {res.error}",
                error_kind="worktree_exit_failed",
            )
        # Prefer the isolator's note — it reports the *actual* outcome, including
        # the fail-safe paths where a requested merge was kept instead (conflict /
        # dirty base) so the model isn't told "merged" when it wasn't.
        if res.note:
            detail = res.note + (f" ({res.commit[:8]})" if res.commit else "")
        else:
            detail = {
                "keep": f"kept branch {res.branch} for manual merge",
                "merge": f"merged branch {res.branch} back to base"
                + (f" ({res.commit[:8]})" if res.commit else ""),
                "discard": "discarded changes and removed the worktree",
            }.get(res.action, res.action)
        return ToolResult.ok(
            f"Exited worktree mode ({res.action}): {detail}. Back on the main work-tree.",
            active=False,
            action=res.action,
            branch=res.branch,
            removed=res.removed,
        )


__all__ = [
    "EnterWorktreeModeArgs",
    "EnterWorktreeModeTool",
    "ExitWorktreeModeArgs",
    "ExitWorktreeModeTool",
]
