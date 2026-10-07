"""ToolDispatcher — the six-stage tool execution middleware chain (docs/reference/tools.md §8).

All cross-cutting concerns (routing, validation, permission gating, eventing,
timeout/abort, error containment) live here so that tool `run()` bodies stay
pure side-effecting logic (P2: tools never call permissions). Stages:

  1. route          -> missing tool returns an error listing available tools
     validate        -> pydantic ValidationError becomes feedback, run() skipped
  2. permission     -> engine consumes `ask` internally; deny short-circuits with
                       a TOOL_START(decision="deny") + TOOL_END(permission_denied)
  3. emit TOOL_START(decision="allow")
  4/5. timeout_for + race_with_abort wraps run(); AbortError/TimeoutError/Exception
       are all contained and rendered as ToolResults (never bubble)
  6. emit TOOL_END derived from the result

`dispatch` fans calls out with `asyncio.gather`, preserving order (T3).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

from pydantic import ValidationError

from autoverse.core.tools.abort_race import abort_result, race_with_abort
from autoverse.core.tools.context import ToolContext
from autoverse.core.tools.middleware import (
    format_unexpected,
    format_validation_error,
    make_reject_message,
    timeout_for,
)
from autoverse.types.abort import AbortError
from autoverse.types.capability import CapabilityTier
from autoverse.types.events import (
    ToolEndPayload,
    ToolStartPayload,
    make_event,
)
from autoverse.types.tools import ToolCall, ToolResult

# Mode-changing tools are barriers: they mutate session execution semantics.
_PLAN_MODE_EXIT = "ExitPlanMode"
_PLAN_MODE_ENTER = "EnterPlanMode"
_MODE_TOOLS: frozenset[str] = frozenset(
    {_PLAN_MODE_ENTER, _PLAN_MODE_EXIT, "EnterWorktreeMode", "ExitWorktreeMode"}
)
_PLAN_UNSAFE_BROWSER_TOOLS: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_new_page",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_keyboard",
        "browser_evaluate",
    }
)

# Long-running orchestration / interaction tools have their own lifecycle:
# sub-agents use role-level safety valves, and AskUserQuestion waits on a human.
# The generic 120s tool wrapper must not cancel them.
_NO_DEFAULT_TIMEOUT: frozenset[str] = frozenset({"agent", "AskUserQuestion"})

if False:  # pragma: no cover - typing aid only
    from autoverse.core.tools.registry import ToolRegistry


class ToolDispatcher:
    def __init__(self, registry: "ToolRegistry", default_timeout: float = 120.0) -> None:
        self._registry = registry
        self._default_timeout = default_timeout

    async def dispatch(
        self, calls: list[ToolCall], ctx: ToolContext
    ) -> list[ToolResult]:
        """Run all calls concurrently, preserving result order (T3)."""
        if not calls:
            return []
        if self._plan_exit_barrier_active(calls, ctx):
            results: list[ToolResult] = []
            exit_seen = False
            for call in calls:
                if call.name == _PLAN_MODE_EXIT and not exit_seen:
                    exit_seen = True
                    results.append(await self.dispatch_one(call, ctx))
                    continue
                results.append(
                    await self._reject(
                        call,
                        ctx,
                        (
                            "Tool call blocked because ExitPlanMode is a barrier. "
                            "The proposed plan must be approved by the user before "
                            "any other tool can run."
                        ),
                        "plan_mode_batch_barrier",
                    )
                )
            return results
        tasks = [self.dispatch_one(c, ctx) for c in calls]
        return list(await asyncio.gather(*tasks))

    async def dispatch_one(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        # ── 1a. route ────────────────────────────────────────────────────────
        tool = self._registry.get(call.name)
        if tool is None:
            available = ", ".join(sorted(self._registry.names())) or "(none)"
            return ToolResult.error(
                f"Unknown tool '{call.name}'. Available tools: {available}.",
                error_kind="unknown_tool",
            )

        # ── 1b. validate ─────────────────────────────────────────────────────
        try:
            args = tool.Args.model_validate(call.input)
        except ValidationError as exc:
            return ToolResult.error(
                format_validation_error(call.name, exc),
                error_kind="invalid_args",
            )

        # Stamp the capability tier from the tool's declaration onto the call so
        # downstream stages (plan-mode gate, permission engine) see the real tier
        # (turn_loop builds ToolCall without a tier, defaulting to READ_ONLY).
        # ToolCall is frozen, so build a tier-stamped copy.
        if call.tier != tool.capability:
            call = dataclasses.replace(call, tier=tool.capability)

        # ── 1b. plan-mode gate ───────────────────────────────────────────────
        # When the REPL is in plan mode (app.extra["plan_mode"].active), hard-
        # block any write-tier tool so the model researches without mutating the
        # workspace (docs/guides/using-cli-and-tui.md §3.10). Read in real time so a mid-session
        # Tab toggle takes effect immediately; ExitPlanMode is always allowed so
        # the model can leave plan mode.
        blocked, reason, kind = self._plan_mode_block_reason(call, tool, ctx)
        if blocked:
            return await self._reject(call, ctx, reason, kind)

        # ── 2. permission ────────────────────────────────────────────────────
        decision = await ctx.permissions.check(call)
        if not decision.allowed:
            reason = decision.reason or "denied"
            await ctx.events.emit(
                make_event(
                    ToolStartPayload(
                        tool_use_id=call.id,
                        tool_name=call.name,
                        tool_input=dict(call.input),
                        decision="deny",
                    )
                )
            )
            result = ToolResult.error(
                make_reject_message(call.name, reason),
                error_kind="permission_denied",
            )
            await self._emit_end(call, result, 0.0, ctx)
            return result

        # ── 3. emit TOOL_START(allow) ────────────────────────────────────────
        await ctx.events.emit(
            make_event(
                ToolStartPayload(
                    tool_use_id=call.id,
                    tool_name=call.name,
                    tool_input=dict(call.input),
                    decision="allow",
                )
            )
        )

        # ── 4/5. timeout + abort race + error containment ────────────────────
        timeout = self._timeout_for_call(call.name, args)
        started = time.monotonic()
        try:
            result = await race_with_abort(
                tool.run(args, ctx), signal=ctx.signal, timeout=timeout
            )
        except AbortError as exc:
            result = abort_result(exc)
        except asyncio.TimeoutError:
            result = ToolResult.error(
                f"Tool '{call.name}' timed out after {timeout:g}s.",
                error_kind="timeout",
            )
        except Exception as exc:  # noqa: BLE001 — contain everything, never bubble
            result = ToolResult.error(
                format_unexpected(call.name, exc),
                error_kind="tool_error",
            )
        duration_ms = (time.monotonic() - started) * 1000.0

        # ── 6. emit TOOL_END ─────────────────────────────────────────────────
        await self._emit_end(call, result, duration_ms, ctx)
        return result

    @classmethod
    def _plan_mode_block_reason(
        cls, call: ToolCall, tool: object, ctx: ToolContext
    ) -> tuple[bool, str, str]:
        """Return whether plan state blocks this call and the user/model feedback."""
        extra = getattr(ctx.app, "extra", None)
        plan_mode = extra.get("plan_mode") if isinstance(extra, dict) else None
        if not isinstance(plan_mode, dict):
            return (False, "", "")
        if plan_mode.get("pending_approval"):
            return (
                True,
                (
                    f"Tool '{call.name}' is blocked while a proposed plan is "
                    "waiting for user approval."
                ),
                "plan_pending_approval",
            )
        if not plan_mode.get("active"):
            return (False, "", "")
        if cls._is_plan_safe(call, tool):
            return (False, "", "")
        return (
            True,
            (
                f"Tool '{call.name}' is blocked in plan mode. Research and present "
                "a plan, then call ExitPlanMode and wait for user approval before "
                "making changes."
            ),
            "plan_mode_blocked",
        )

    @classmethod
    def _is_plan_safe(cls, call: ToolCall, tool: object) -> bool:
        """Runtime plan-safe check; narrower than READ_ONLY."""
        if call.name == _PLAN_MODE_EXIT:
            return True
        if call.name in _MODE_TOOLS:
            return False
        if call.name in _PLAN_UNSAFE_BROWSER_TOOLS:
            return False
        if call.name == "bash":
            command = str(call.input.get("command") or "")
            try:
                from autoverse.core.tools.builtin.shell import classify_command

                _kind, readonly = classify_command(command)
                return readonly
            except Exception:  # noqa: BLE001 - fail closed on classifier bugs
                return False
        cap = getattr(tool, "capability", CapabilityTier.READ_ONLY)
        return cap < CapabilityTier.CONTROLLED_WRITE

    @staticmethod
    def _plan_exit_barrier_active(calls: list[ToolCall], ctx: ToolContext) -> bool:
        if len(calls) < 2 or not any(c.name == _PLAN_MODE_EXIT for c in calls):
            return False
        extra = getattr(ctx.app, "extra", None)
        plan_mode = extra.get("plan_mode") if isinstance(extra, dict) else None
        return bool(isinstance(plan_mode, dict) and plan_mode.get("active"))

    @staticmethod
    def _requested_timeout(args: object) -> float | None:
        val = getattr(args, "timeout", None)
        if isinstance(val, (int, float)):
            return float(val)
        return None

    def _timeout_for_call(self, tool_name: str, args: object) -> float | None:
        requested = self._requested_timeout(args)
        if requested is None and tool_name in _NO_DEFAULT_TIMEOUT:
            return None
        return timeout_for(tool_name, requested, self._default_timeout)

    @staticmethod
    async def _emit_end(
        call: ToolCall, result: ToolResult, duration_ms: float, ctx: ToolContext
    ) -> None:
        await ctx.events.emit(
            make_event(
                ToolEndPayload(
                    tool_use_id=call.id,
                    tool_name=call.name,
                    output=result.output,
                    is_error=result.is_error,
                    error_kind=result.error_kind,
                    error_message=result.output if result.is_error else "",
                    duration_ms=duration_ms,
                    metadata=dict(result.metadata),
                )
            )
        )

    @staticmethod
    async def _reject(
        call: ToolCall, ctx: ToolContext, message: str, error_kind: str
    ) -> ToolResult:
        await ctx.events.emit(
            make_event(
                ToolStartPayload(
                    tool_use_id=call.id,
                    tool_name=call.name,
                    tool_input=dict(call.input),
                    decision="deny",
                )
            )
        )
        result = ToolResult.error(message, error_kind=error_kind)
        await ToolDispatcher._emit_end(call, result, 0.0, ctx)
        return result


__all__ = ["ToolDispatcher"]
